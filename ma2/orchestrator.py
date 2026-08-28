"""编排器。

第二步的核心约束：**worktree 串行创建，Agent 并行执行。**

`git worktree add` 会写目标仓库的 refs 和 index，并发调用必然撞 index.lock。
所以执行分三个阶段：

    1. prepare   串行。逐个建工作区，任何一个失败就整体中止，不留半拉子现场。
    2. dispatch  并行。ThreadPoolExecutor，每个 Agent 一个线程。
                 Agent 是 I/O 密集（阻塞在子进程管道上），线程模型足够。
    3. collect   串行。收集 worktree 改动，写聚合状态，按需回收。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import protocol as P
from . import workspace as W
from .runner import run_agent


class Orchestrator:
    def __init__(self, plan: dict[str, Any], paths: P.RunPaths, *,
                 max_parallel: int = 3, forward_anthropic: bool = False,
                 quiet: bool = False, cleanup: bool = False,
                 autocommit: bool = True):
        self.plan = plan
        self.paths = paths
        self.tasks: list[dict[str, Any]] = plan.get("tasks") or []
        self.max_parallel = max(1, int(plan.get("max_parallel", max_parallel)))
        self.forward_anthropic = forward_anthropic
        self.quiet = quiet
        self.cleanup = cleanup
        self.autocommit = autocommit

        self.workspaces: dict[str, W.Workspace] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}

        # 并行下 print 会互相撕裂；聚合状态是多线程共享写，也要保护
        self._io_lock = threading.Lock()
        self._agg_lock = threading.Lock()

    # ------------------------------------------------------------------ 输出
    def log(self, agent_id: str, msg: str) -> None:
        if self.quiet:
            return
        with self._io_lock:
            print(f"[{agent_id:<12}] {msg}", flush=True)

    def note(self, msg: str) -> None:
        if self.quiet:
            return
        with self._io_lock:
            print(msg, flush=True)

    # ------------------------------------------------------- 阶段 1：串行准备
    def prepare(self) -> None:
        defaults = self.plan.get("workspace_defaults") or {}
        if self.plan.get("repo") and "repo" not in defaults:
            defaults["repo"] = self.plan["repo"]

        self.note(f"阶段 1/3  准备 {len(self.tasks)} 个工作区（串行）")
        for task in self.tasks:
            agent_id = task["id"]
            spec = W.resolve_spec(task, defaults)
            dest = self.paths.workspace(agent_id)
            try:
                ws = W.prepare(agent_id, spec, dest, self.paths.run_id)
            except W.WorkspaceError:
                # 已建好的要回收掉，不留半拉子现场
                self.rollback_workspaces()
                raise
            self.workspaces[agent_id] = ws
            if ws.kind == W.WORKTREE:
                self.log(agent_id, f"worktree  {ws.branch}  <- {ws.base_ref} @ {(ws.head or '')[:8]}")
            else:
                self.log(agent_id, f"plain     {dest.name}")

    def rollback_workspaces(self) -> None:
        for ws in self.workspaces.values():
            W.remove(ws, delete_branch=True)
        self.workspaces.clear()

    # ------------------------------------------------------- 阶段 2：并行派发
    def dispatch(self) -> None:
        n = len(self.tasks)
        self.note(f"\n阶段 2/3  并行派发 {n} 个 Agent（max_parallel={self.max_parallel}）")

        def work(task: dict[str, Any]) -> dict[str, Any]:
            agent_id = task["id"]
            return run_agent(
                task, self.paths,
                workspace=self.workspaces[agent_id].path,
                forward_anthropic=self.forward_anthropic,
                log=lambda m, _id=agent_id: self.log(_id, m),
                on_update=self.on_agent_update,
            )

        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            futures = {pool.submit(work, t): t["id"] for t in self.tasks}
            for fut in as_completed(futures):
                agent_id = futures[fut]
                try:
                    self.results[agent_id] = fut.result()
                except Exception as exc:  # 单个 Agent 崩溃不能拖垮整个 run
                    self.log(agent_id, f"执行器异常: {exc!r}")
                    self.results[agent_id] = {
                        "agent_id": agent_id, "run_id": self.paths.run_id,
                        "status": P.FAILED, "answer": None,
                        "metrics": {}, "diagnostics": {"executor_error": repr(exc)},
                    }

    def on_agent_update(self, agent_id: str, snapshot: dict[str, Any]) -> None:
        """任一 Agent 状态变化就刷新聚合状态，供观察面读取。"""
        with self._agg_lock:
            self.snapshots[agent_id] = snapshot
            P.write_atomic(self.paths.run_status, {
                "run_id": self.paths.run_id,
                "updated_at": P.utcnow(),
                "max_parallel": self.max_parallel,
                "agents": [self.snapshots[t["id"]]
                           for t in self.tasks if t["id"] in self.snapshots],
            })

    # ------------------------------------------------------- 阶段 3：串行收尾
    def collect(self) -> dict[str, Any]:
        self.note("\n阶段 3/3  收集改动")
        for task in self.tasks:
            agent_id = task["id"]
            ws = self.workspaces.get(agent_id)
            if ws is None:
                continue

            # 先提交再收集：否则 committed 永远是 False，且工作区一旦回收，
            # Agent 的改动就随 --force 一起没了。
            commit_info: dict[str, Any] = {}
            if self.autocommit:
                status = (self.results.get(agent_id) or {}).get("status", "?")
                commit_info = W.commit_changes(
                    ws, f"[ma2] {agent_id} @ {self.paths.run_id} ({status})"
                )
                if commit_info.get("committed"):
                    self.log(agent_id, f"commit  {commit_info['commit'][:8]}")

            changes = W.collect_changes(ws)
            result = self.results.get(agent_id)
            if result is not None:
                result.setdefault("workspace", {}).update({
                    "kind": ws.kind, "path": str(ws.path),
                    "repo": str(ws.repo) if ws.repo else None,
                    **changes,
                    **({"autocommit": commit_info} if commit_info else {}),
                })
                P.write_atomic(self.paths.result(agent_id), result)
            if changes:
                self.log(agent_id, f"branch={changes.get('branch')} "
                                   f"dirty={changes.get('dirty_files')} "
                                   f"committed={changes.get('committed')}")

        if self.cleanup:
            for agent_id, ws in self.workspaces.items():
                self.log(agent_id, f"worktree {W.remove(ws)}")

        ordered = [self.results[t["id"]] for t in self.tasks if t["id"] in self.results]
        ok = sum(1 for r in ordered if r.get("status") == P.COMPLETED)
        summary = {
            "run_id": self.paths.run_id,
            "run_name": self.plan.get("run_name"),
            "finished_at": P.utcnow(),
            "status": P.COMPLETED if ok == len(ordered) and ordered else P.FAILED,
            "completed": ok,
            "total": len(ordered),
            "agents": [{
                "agent_id": r.get("agent_id"),
                "status": r.get("status"),
                "branch": (r.get("workspace") or {}).get("branch"),
                "dirty_files": (r.get("workspace") or {}).get("dirty_files"),
                "duration_ms": (r.get("metrics") or {}).get("duration_ms"),
                "total_cost_usd": (r.get("metrics") or {}).get("total_cost_usd"),
                "permission_denials": len((r.get("diagnostics") or {}).get("permission_denials") or []),
            } for r in ordered],
        }
        P.write_atomic(self.paths.run_json, {
            **(P.read_json(self.paths.run_json) or {}), **summary,
        })
        return summary

    # ------------------------------------------------------------------ 总入口
    def execute(self) -> dict[str, Any]:
        self.prepare()
        self.dispatch()
        return self.collect()
