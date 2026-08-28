"""Agent 工作区准备与回收。

对应 handoff.md §10 文件隔离。两种工作区：

    plain     —— 普通空目录。调研、检索、写文档这类不碰代码库的任务用这个。
    worktree  —— 目标仓库的 git worktree，独立分支、独立工作目录。
                 多个 Agent 改同一个代码库时必须用它。

关键约束：`git worktree add` 会写目标仓库的 refs 与 index，**并发调用会撞
index.lock**。因此所有 worktree 必须在派发 Agent 之前串行创建完毕，不能放进
并行阶段。这是本模块存在的主要理由。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLAIN = "plain"
WORKTREE = "worktree"

_SAFE = re.compile(r"[^A-Za-z0-9._/-]")


class WorkspaceError(RuntimeError):
    pass


@dataclass
class Workspace:
    agent_id: str
    kind: str
    path: Path
    repo: Path | None = None
    branch: str | None = None
    base_ref: str | None = None
    head: str | None = None


def _git(repo: Path, *args: str) -> str:
    """调用 git。

    core.quotePath=false 是必须的：默认情况下 git 会把非 ASCII 路径转义成
    八进制（`"\\346\\226\\260.md"`），这些字符串会原样进 result.json，
    汇总层拿到的就是乱码。
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotePath=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} 失败 (code={proc.returncode})\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def sanitize_branch(name: str) -> str:
    return _SAFE.sub("-", name).strip("-/") or "agent"


def resolve_spec(task: dict[str, Any], run_defaults: dict[str, Any]) -> dict[str, Any]:
    """任务级 workspace 配置，缺省时回退到 run 级。"""
    spec = task.get("workspace")
    if isinstance(spec, str):
        spec = {"kind": spec}
    spec = dict(spec or {})
    if "kind" not in spec:
        spec["kind"] = run_defaults.get("kind", PLAIN)
    for key in ("repo", "base_ref", "branch_prefix"):
        if key not in spec and key in run_defaults:
            spec[key] = run_defaults[key]
    return spec


def prepare(agent_id: str, spec: dict[str, Any], dest: Path, run_id: str) -> Workspace:
    """创建一个 Agent 的工作区。必须串行调用。"""
    kind = spec.get("kind", PLAIN)

    if kind == PLAIN:
        dest.mkdir(parents=True, exist_ok=True)
        return Workspace(agent_id=agent_id, kind=PLAIN, path=dest)

    if kind != WORKTREE:
        raise WorkspaceError(f"未知的 workspace kind: {kind!r}")

    repo_raw = spec.get("repo")
    if not repo_raw:
        raise WorkspaceError(f"[{agent_id}] workspace.kind=worktree 但没给 repo")
    repo = Path(repo_raw).expanduser().resolve()
    if not (repo / ".git").exists():
        raise WorkspaceError(f"[{agent_id}] 不是 git 仓库: {repo}")

    base_ref = spec.get("base_ref") or "HEAD"
    prefix = spec.get("branch_prefix") or "ma2"
    branch = spec.get("branch") or f"{prefix}/{run_id}/{agent_id}"
    branch = sanitize_branch(branch)

    # worktree 目标目录必须不存在，交给 git 自己建
    if dest.exists():
        raise WorkspaceError(f"[{agent_id}] worktree 目标已存在: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    _git(repo, "worktree", "add", "-b", branch, str(dest), base_ref)
    head = _git(dest, "rev-parse", "HEAD")

    return Workspace(
        agent_id=agent_id, kind=WORKTREE, path=dest,
        repo=repo, branch=branch, base_ref=base_ref, head=head,
    )


def collect_changes(ws: Workspace) -> dict[str, Any]:
    """收集 worktree 里的实际改动。汇总层判断 Agent 是否真的动了代码。"""
    if ws.kind != WORKTREE or ws.repo is None:
        return {}
    try:
        porcelain = _git(ws.path, "status", "--porcelain")
        head_now = _git(ws.path, "rev-parse", "HEAD")
    except WorkspaceError as exc:
        return {"error": str(exc)}

    dirty = [ln for ln in porcelain.splitlines() if ln.strip()]
    return {
        "branch": ws.branch,
        "base_ref": ws.base_ref,
        "head_at_start": ws.head,
        "head_now": head_now,
        "committed": head_now != ws.head,
        "dirty_files": len(dirty),
        "dirty": dirty[:50],
    }


def commit_changes(ws: Workspace, message: str) -> dict[str, Any]:
    """把 Agent 留在工作区里的改动提交到它自己的分支上。

    这一步不是可选的收尾动作，而是隔离模型成立的前提。Agent 几乎不会自己
    `git commit`，成果只存在于工作目录；而 `worktree remove` 需要 --force 才
    能删掉脏工作区，一删就把改动一起删了 —— "保留分支"于是成为空承诺。
    先提交，分支才真正持有交付物，工作区才成为一次性的。

    提交者身份显式指定：编排器不该依赖跑它的那台机器恰好配了 user.name。
    """
    if ws.kind != WORKTREE or ws.repo is None:
        return {"committed": False, "reason": "not a worktree"}
    try:
        if not _git(ws.path, "status", "--porcelain"):
            return {"committed": False, "reason": "工作区干净，无需提交"}
        _git(ws.path, "add", "-A")
        _git(
            ws.path,
            "-c", "user.name=ma2-orchestrator",
            "-c", "user.email=ma2@localhost",
            "commit", "-m", message, "--no-verify",
        )
        return {"committed": True, "commit": _git(ws.path, "rev-parse", "HEAD")}
    except WorkspaceError as exc:
        return {"committed": False, "reason": str(exc)}


def is_dirty(ws: Workspace) -> bool:
    if ws.kind != WORKTREE or ws.repo is None:
        return False
    try:
        return bool(_git(ws.path, "status", "--porcelain"))
    except WorkspaceError:
        return False


def remove(ws: Workspace, *, delete_branch: bool = False) -> str:
    """回收 worktree。

    默认保留分支。但只有先 commit_changes 过，分支上才真有东西 ——
    --force 会连未提交的改动一起删除。
    """
    if ws.kind != WORKTREE or ws.repo is None:
        return "skipped"
    try:
        _git(ws.repo, "worktree", "remove", "--force", str(ws.path))
        if delete_branch and ws.branch:
            _git(ws.repo, "branch", "-D", ws.branch)
        return "removed"
    except WorkspaceError as exc:
        return f"failed: {exc}"
