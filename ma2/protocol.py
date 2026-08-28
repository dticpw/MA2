"""文件协议层。

对应 handoff.md §6：所有跨进程状态都落成磁盘上的 JSON，写入必须原子。
编排器与 Agent 之间不通过屏幕文本交换任何正式结果。

目录布局：

    runs/<run_id>/
        run.json                  运行元数据
        agents/<agent_id>.jsonl   Agent 原始事件流（审计依据，只追加不修改）
        agents/<agent_id>.status.json
        agents/<agent_id>.result.json
        agents/<agent_id>.stderr.log
        workspaces/<agent_id>/    Agent 工作目录
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- 状态枚举
# handoff.md §11。terminal 集合以外的状态都意味着编排器仍需继续观察。
PENDING = "pending"
STARTING = "starting"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"

TERMINAL = frozenset({COMPLETED, FAILED, TIMEOUT, CANCELLED})


def utcnow() -> str:
    """ISO-8601 UTC，毫秒精度。全项目统一时间格式。"""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def write_atomic(path: Path, data: dict) -> None:
    """先写同目录临时文件再 os.replace。

    同目录是必要条件：跨卷的 replace 不是原子操作。读者要么看到旧的完整
    版本，要么看到新的完整版本，不会读到写了一半的 JSON。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 失败时不要留下半截临时文件污染目录
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


class RunPaths:
    """一次 run 的全部路径。别处不要手工拼路径。"""

    def __init__(self, root: Path, run_id: str):
        self.root = Path(root)
        self.run_id = run_id
        self.dir = self.root / run_id
        self.agents = self.dir / "agents"
        self.workspaces = self.dir / "workspaces"
        self.run_json = self.dir / "run.json"

    def events(self, agent_id: str) -> Path:
        return self.agents / f"{agent_id}.jsonl"

    def status(self, agent_id: str) -> Path:
        return self.agents / f"{agent_id}.status.json"

    def result(self, agent_id: str) -> Path:
        return self.agents / f"{agent_id}.result.json"

    def stderr(self, agent_id: str) -> Path:
        return self.agents / f"{agent_id}.stderr.log"

    def workspace(self, agent_id: str) -> Path:
        return self.workspaces / agent_id

    def ensure(self) -> None:
        self.agents.mkdir(parents=True, exist_ok=True)
        self.workspaces.mkdir(parents=True, exist_ok=True)
