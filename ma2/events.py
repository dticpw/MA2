"""stream-json 事件归约。

事件 schema 来自 2026-08-28 对 `claude -p --output-format stream-json --verbose`
的实测，claude 2.1.250。观测到的四类事件：

    {"type":"system","subtype":"init", session_id, cwd, model, permissionMode, tools[...]}
    {"type":"assistant", message:{content:[{type:"tool_use"|"text",...}]}, timestamp, uuid}
    {"type":"user",      message:{content:[{type:"tool_result",...}]}, tool_use_result}
    {"type":"result","subtype":"success"|..., is_error, stop_reason, terminal_reason,
                     num_turns, duration_ms, total_cost_usd, usage, permission_denials, result}

这一层只做归约，不做 IO，方便单测。它是整个系统判定 Agent 状态的唯一依据 ——
不抓屏、不匹配终端缓冲区、不猜进程状态（handoff.md §15）。
"""

from __future__ import annotations

from typing import Any

from . import protocol as P


class AgentState:
    """对单个 Agent 的事件流做增量归约。

    每来一行调一次 update()，随时可以 snapshot() 出当前状态。
    """

    def __init__(self, agent_id: str, run_id: str, attempt: int = 1):
        self.agent_id = agent_id
        self.run_id = run_id
        self.attempt = attempt
        self.status = P.STARTING

        self.session_id: str | None = None
        self.model: str | None = None
        self.cwd: str | None = None
        self.permission_mode: str | None = None

        self.event_count = 0
        self.malformed_lines = 0
        self.turns_seen = 0
        self.tool_calls: list[str] = []
        self.last_activity: str | None = None
        self.last_event_at: str | None = None

        # 终态字段，仅在 result 事件到达后填充
        self.is_error: bool | None = None
        self.result_subtype: str | None = None
        self.stop_reason: str | None = None
        self.terminal_reason: str | None = None
        self.num_turns: int | None = None
        self.duration_ms: int | None = None
        self.duration_api_ms: int | None = None
        self.total_cost_usd: float | None = None
        self.usage: dict[str, Any] | None = None
        self.permission_denials: list[Any] = []
        self.answer: str | None = None

        self.started_at = P.utcnow()
        self.ended_at: str | None = None

    # ------------------------------------------------------------------ 归约
    def update(self, event: dict[str, Any]) -> None:
        self.event_count += 1
        etype = event.get("type")
        ts = event.get("timestamp")
        if ts:
            self.last_event_at = ts

        if etype == "system" and event.get("subtype") == "init":
            self.session_id = event.get("session_id")
            self.model = event.get("model")
            self.cwd = event.get("cwd")
            self.permission_mode = event.get("permissionMode")
            self.status = P.RUNNING
            self.last_activity = "init"

        elif etype == "assistant":
            self.status = P.RUNNING
            self.turns_seen += 1
            for block in _content(event):
                if block.get("type") == "tool_use":
                    name = block.get("name") or "?"
                    self.tool_calls.append(name)
                    self.last_activity = f"tool:{name}"
                elif block.get("type") == "text":
                    self.last_activity = "text"

        elif etype == "user":
            # tool_result 回灌。仍在跑，只更新活动描述。
            for block in _content(event):
                if block.get("type") == "tool_result":
                    self.last_activity = "tool_result"

        elif etype == "result":
            self._absorb_result(event)

    def _absorb_result(self, event: dict[str, Any]) -> None:
        self.is_error = bool(event.get("is_error"))
        self.result_subtype = event.get("subtype")
        self.stop_reason = event.get("stop_reason")
        self.terminal_reason = event.get("terminal_reason")
        self.num_turns = event.get("num_turns")
        self.duration_ms = event.get("duration_ms")
        self.duration_api_ms = event.get("duration_api_ms")
        self.total_cost_usd = event.get("total_cost_usd")
        self.usage = event.get("usage")
        self.permission_denials = event.get("permission_denials") or []
        self.answer = event.get("result")
        self.ended_at = P.utcnow()
        self.status = P.FAILED if self.is_error else P.COMPLETED
        self.last_activity = "result"

    def mark(self, status: str, note: str | None = None) -> None:
        """外部强制置终态（超时、取消、进程异常退出）。"""
        self.status = status
        self.ended_at = self.ended_at or P.utcnow()
        if note:
            self.last_activity = note

    # ------------------------------------------------------------------ 快照
    def snapshot(self) -> dict[str, Any]:
        """写入 status.json 的内容。刻意保持小而廉价，因为它高频写。"""
        return {
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "status": self.status,
            "session_id": self.session_id,
            "model": self.model,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "updated_at": P.utcnow(),
            "event_count": self.event_count,
            "malformed_lines": self.malformed_lines,
            "turns_seen": self.turns_seen,
            "tool_calls": len(self.tool_calls),
            "last_activity": self.last_activity,
            "last_event_at": self.last_event_at,
        }

    def result_document(self, exit_code: int | None, events_path: str) -> dict[str, Any]:
        """写入 result.json 的内容。这是交给汇总层的正式产物（§6 / §13-8）。"""
        return {
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "status": self.status,
            "session_id": self.session_id,
            "model": self.model,
            "answer": self.answer,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": exit_code,
            "metrics": {
                "num_turns": self.num_turns,
                "turns_seen": self.turns_seen,
                "duration_ms": self.duration_ms,
                "duration_api_ms": self.duration_api_ms,
                "total_cost_usd": self.total_cost_usd,
                "usage": self.usage,
                "event_count": self.event_count,
                "malformed_lines": self.malformed_lines,
            },
            "diagnostics": {
                "is_error": self.is_error,
                "result_subtype": self.result_subtype,
                "stop_reason": self.stop_reason,
                "terminal_reason": self.terminal_reason,
                "permission_denials": self.permission_denials,
                "tool_calls": self.tool_calls,
            },
            "events_path": events_path,
        }


def _content(event: dict[str, Any]) -> list[dict[str, Any]]:
    msg = event.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]
