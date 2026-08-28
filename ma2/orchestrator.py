"""编排器。

第二步的核心约束：**worktree 串行创建，Agent 并行执行。**

`git worktree add` 会写目标仓库的 refs 和 index，并发调用必然撞 index.lock。
所以执行分四个阶段：

    1. prepare    串行。逐个建工作区，任何一个失败就整体中止，不留半拉子现场。
    2. dispatch   并行。ThreadPoolExecutor，每个 Agent 一个线程。
                  Agent 是 I/O 密集（阻塞在子进程管道上），线程模型足够。
    3. collect    串行。提交改动到各自分支，收集 diff，写聚合状态，按需回收。
    4. aggregate  串行。N 份 result.json → final.md。

第三步加入的是**判定与重试**（见 ma2.policy）：dispatch 里每个 Agent 跑的不再是
一次 run_agent，而是"跑 → 判定 → 该重试就重置工作区再跑"的循环。汇总口径也
随之从 status 改成 verdict。

第四步加入的是**汇总**（见 ma2.aggregate）。它必须排在 collect 之后，因为产出
要先被提交到分支上才读得到 —— 阶段 4 只从分支读，不碰工作目录。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import aggregate as AG
from . import policy as PO
from . import protocol as P
from . import workspace as W
from .runner import run_agent


class Orchestrator:
    def __init__(self, plan: dict[str, Any], paths: P.RunPaths, *,
                 max_parallel: int = 3, forward_anthropic: bool = False,
                 quiet: bool = False, cleanup: bool = False,
                 autocommit: bool = True, aggregate_with_agent: bool | None = None):
        self.plan = plan
        self.paths = paths
        self.tasks: list[dict[str, Any]] = plan.get("tasks") or []
        self.max_parallel = max(1, int(plan.get("max_parallel", max_parallel)))
        self.forward_anthropic = forward_anthropic
        self.quiet = quiet
        self.cleanup = cleanup
        self.autocommit = autocommit
        # 综合层要花钱，因此显式开启：plan 里给了 aggregator，或命令行 --aggregate。
        # 机械层不受这个开关影响，永远产出。
        self.aggregate_with_agent = (
            bool(plan.get("aggregator")) if aggregate_with_agent is None
            else aggregate_with_agent
        )

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

        self.note(f"阶段 1/4  准备 {len(self.tasks)} 个工作区（串行）")
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
        self.note(f"\n阶段 2/4  并行派发 {n} 个 Agent（max_parallel={self.max_parallel}）")

        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            futures = {pool.submit(self.run_with_retry, t): t["id"] for t in self.tasks}
            for fut in as_completed(futures):
                agent_id = futures[fut]
                try:
                    self.results[agent_id] = fut.result()
                except Exception as exc:  # 单个 Agent 崩溃不能拖垮整个 run
                    self.log(agent_id, f"执行器异常: {exc!r}")
                    self.results[agent_id] = {
                        "agent_id": agent_id, "run_id": self.paths.run_id,
                        "status": P.FAILED, "answer": None, "attempt": 0,
                        "verdict": {"ok": False, "reasons": [PO.CRASHED],
                                    "retryable": False},
                        "metrics": {}, "diagnostics": {"executor_error": repr(exc)},
                    }

    def run_with_retry(self, task: dict[str, Any]) -> dict[str, Any]:
        """跑一个 Agent，按策略重试，返回带 verdict 的最终 result。

        重试判据来自 ma2.policy，不在这里现编。关键一条：权限被拒不重试 ——
        同样的 allowed_tools 重试多少次都会被同样拦下，重试只是把钱烧两遍。
        """
        agent_id = task["id"]
        ws = self.workspaces[agent_id]
        checks = PO.resolve_checks(task, self.plan)
        max_attempts, delay, do_reset = PO.retry_plan(task, self.plan)

        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        verdict = PO.Verdict(ok=False, reasons=[PO.CRASHED])

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self.log(agent_id, f"retry   第 {attempt}/{max_attempts} 次尝试"
                                   f"（上次: {'+'.join(verdict.reasons)}）")
                if do_reset:
                    info = W.reset(ws)
                    if info.get("discarded"):
                        self.log(agent_id, f"reset   丢弃上次尝试的 "
                                           f"{info['discarded']} 处改动")
                if delay:
                    time.sleep(delay * (attempt - 1))  # 线性退避

            result = run_agent(
                task, self.paths, workspace=ws.path, attempt=attempt,
                forward_anthropic=self.forward_anthropic,
                log=lambda m, _id=agent_id: self.log(_id, m),
                on_update=self.on_agent_update,
            )

            # require_changes 需要看工作区。git status 在各自的 worktree 里跑，
            # 索引是每 worktree 独立的，并发不会撞 index.lock。
            changes = W.collect_changes(ws) if checks.get("require_changes") else None
            verdict = PO.evaluate(result, checks=checks, changes=changes)

            attempts.append({
                "attempt": attempt,
                "status": result.get("status"),
                "verdict": verdict.to_dict(),
                "events_path": result.get("events_path"),
                "duration_ms": (result.get("metrics") or {}).get("duration_ms"),
                "wall_ms": (result.get("metrics") or {}).get("wall_ms"),
                "total_cost_usd": (result.get("metrics") or {}).get("total_cost_usd"),
            })

            if verdict.ok:
                break
            if not verdict.retryable:
                self.log(agent_id, f"verdict 失败且不可重试: {'+'.join(verdict.reasons)}")
                break
            if attempt == max_attempts:
                self.log(agent_id, f"verdict 重试 {max_attempts} 次仍失败: "
                                   f"{'+'.join(verdict.reasons)}")

        result["attempt"] = len(attempts)
        result["attempts"] = attempts
        result["verdict"] = verdict.to_dict()
        # 重试会重复计费，只报最后一次是在瞒账
        result.setdefault("metrics", {})["cost_all_attempts_usd"] = sum(
            a["total_cost_usd"] or 0.0 for a in attempts)
        result["metrics"]["wall_ms_all_attempts"] = sum(
            a["wall_ms"] or 0 for a in attempts)
        # 超时的尝试收不到 result 事件，费用无从得知。它们在上面按 0 计入，
        # 于是这个和是**下界而不是事实** —— 必须标出来，否则打印 $0.0000
        # 会让人以为那次失败是免费的。耗时能靠墙钟自测，费用不能。
        result["metrics"]["cost_is_complete"] = all(
            a["total_cost_usd"] is not None for a in attempts)
        P.write_atomic(self.paths.result(agent_id), result)
        return result

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
        self.note("\n阶段 3/4  收集改动")
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
        # 汇总按 verdict 算，不按 status 算。README 结论 3：一次被权限全拦下的
        # 运行 status 也是 completed，按 status 汇总就是把失败报成成功。
        ok = sum(1 for r in ordered if (r.get("verdict") or {}).get("ok"))
        completed = sum(1 for r in ordered if r.get("status") == P.COMPLETED)
        summary = {
            "run_id": self.paths.run_id,
            "run_name": self.plan.get("run_name"),
            "finished_at": P.utcnow(),
            "status": P.COMPLETED if ok == len(ordered) and ordered else P.FAILED,
            "ok": ok,
            "completed": completed,
            "total": len(ordered),
            "total_cost_usd": sum((r.get("metrics") or {}).get("cost_all_attempts_usd") or 0.0
                                  for r in ordered),
            "cost_is_complete": all((r.get("metrics") or {}).get("cost_is_complete", True)
                                    for r in ordered),
            "agents": [{
                "agent_id": r.get("agent_id"),
                "status": r.get("status"),
                "ok": bool((r.get("verdict") or {}).get("ok")),
                "reasons": (r.get("verdict") or {}).get("reasons") or [],
                "attempt": r.get("attempt"),
                "branch": (r.get("workspace") or {}).get("branch"),
                "dirty_files": (r.get("workspace") or {}).get("dirty_files"),
                # 用墙钟而不是 result 事件里的 duration_ms，理由有两条：超时的运行
                # 根本收不到 result 事件（duration_ms 是 None，一次真烧了钱的失败
                # 会在表里显示成 0.0s），而重试过的运行只报最后一次也是在瞒账 ——
                # 和 cost_all_attempts_usd 一个口径。
                "duration_ms": ((r.get("metrics") or {}).get("wall_ms_all_attempts")
                                or (r.get("metrics") or {}).get("wall_ms")
                                or (r.get("metrics") or {}).get("duration_ms")),
                "api_duration_ms": (r.get("metrics") or {}).get("duration_ms"),
                "total_cost_usd": (r.get("metrics") or {}).get("cost_all_attempts_usd"),
                "cost_is_complete": (r.get("metrics") or {}).get("cost_is_complete", True),
                "permission_denials": len((r.get("diagnostics") or {}).get("permission_denials") or []),
            } for r in ordered],
        }
        P.write_atomic(self.paths.run_json, {
            **(P.read_json(self.paths.run_json) or {}), **summary,
        })
        return summary

    # ------------------------------------------------------- 阶段 4：串行汇总
    def aggregate(self, summary: dict[str, Any]) -> dict[str, Any]:
        """把 N 份 result.json 汇总成 final.md。

        必须排在 collect 之后：产出要先被提交到分支上，这里才读得到
        （结论 7）。也正因为读的是分支，`--cleanup` 把工作区收掉之后汇总
        照样成立。

        机械层无条件产出，综合层显式开启 —— 见 ma2.aggregate 的模块说明。
        """
        ordered = [self.results[t["id"]] for t in self.tasks if t["id"] in self.results]
        self.note("\n阶段 4/4  汇总")
        records = AG.gather(ordered, self.tasks)
        brief = AG.build_brief(self.paths.run_id, self.plan.get("run_name"), records)
        self.paths.brief.write_text(brief, encoding="utf-8")

        synthesis: dict[str, Any] | None = None
        if self.aggregate_with_agent:
            synthesis = self.run_aggregator(brief)
            # 汇总器不算进 ok/total —— 它不是参与运算的 Agent，算进去会把
            # "3/3 成功"写成"3/4"。但它**确实花了钱**：费用不并进总额，
            # 打出来的总价就比实际少一个 Agent，那还是账面比现实好看。
            # 两个口径都留着：agents_cost_usd 是干活的部分，total 是这次
            # run 真实的开销。
            cost = synthesis.get("cost_usd")
            summary["agents_cost_usd"] = summary.get("total_cost_usd")
            summary["aggregator_cost_usd"] = cost
            summary["total_cost_usd"] = (summary.get("total_cost_usd") or 0.0) + (cost or 0.0)
            if not synthesis.get("cost_is_complete", True):
                # 汇总器超时/崩溃时费用无从得知，按 0 计入，总额就此变成下界
                summary["cost_is_complete"] = False

        self.paths.final.write_text(
            AG.render_final(self.paths.run_id, self.plan.get("run_name"),
                            summary, records, synthesis),
            encoding="utf-8",
        )
        self.note(f"\n汇总   {self.paths.final}")
        summary["final"] = str(self.paths.final)
        summary["brief"] = str(self.paths.brief)
        if synthesis is not None:
            summary["aggregator"] = synthesis
        P.write_atomic(self.paths.run_json, {
            **(P.read_json(self.paths.run_json) or {}), **summary,
        })
        return summary

    def run_aggregator(self, brief: str) -> dict[str, Any]:
        """跑综合层。它就是一个普通 Agent：同一套超时、判定与重试。

        失败不抛异常 —— 综合失败只该让 final.md 少一段正文，不该让整次 run
        的收尾崩掉。各 Agent 的产出已经在各自分支上了。
        """
        task = AG.aggregator_task(self.plan, brief)
        agent_id = task["id"]
        dest = self.paths.workspace(agent_id)
        dest.mkdir(parents=True, exist_ok=True)
        self.workspaces[agent_id] = W.Workspace(agent_id, W.PLAIN, dest)
        self.note(f"\n阶段 4/4  综合汇总（{agent_id}）")
        try:
            result = self.run_with_retry(task)
        except Exception as exc:  # 总结器崩了不能拖垮收尾
            self.log(agent_id, f"执行器异常: {exc!r}")
            return {"ok": False, "reasons": [PO.CRASHED], "status": P.FAILED,
                    "attempt": 0, "answer": None, "error": repr(exc),
                    "cost_usd": 0.0, "cost_is_complete": False}
        verdict = result.get("verdict") or {}
        metrics = result.get("metrics") or {}
        return {
            "ok": bool(verdict.get("ok")),
            "reasons": verdict.get("reasons") or [],
            "status": result.get("status"),
            "attempt": result.get("attempt"),
            "answer": result.get("answer"),
            "cost_usd": metrics.get("cost_all_attempts_usd"),
            "cost_is_complete": metrics.get("cost_is_complete", True),
        }

    # ------------------------------------------------------------------ 总入口
    def execute(self) -> dict[str, Any]:
        self.prepare()
        self.dispatch()
        return self.aggregate(self.collect())
