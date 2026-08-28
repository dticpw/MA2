"""事件归约。这是系统判定 Agent 状态的唯一依据，所以测得细一点。"""

from __future__ import annotations

import unittest

from ma2 import protocol as P
from ma2.events import AgentState


def init(**over):
    ev = {"type": "system", "subtype": "init", "session_id": "s1",
          "cwd": "C:/ws", "model": "m", "permissionMode": "acceptEdits"}
    ev.update(over)
    return ev


def result(**over):
    ev = {"type": "result", "subtype": "success", "is_error": False,
          "stop_reason": "end_turn", "terminal_reason": "done",
          "num_turns": 2, "duration_ms": 900, "total_cost_usd": 0.01,
          "usage": {"input_tokens": 1}, "permission_denials": [],
          "result": "答案"}
    ev.update(over)
    return ev


class TestReduction(unittest.TestCase):
    def setUp(self) -> None:
        self.st = AgentState("a1", "r1")

    def test_starts_in_starting(self) -> None:
        self.assertEqual(self.st.status, P.STARTING)
        self.assertIsNone(self.st.ended_at)

    def test_init_captures_session_and_goes_running(self) -> None:
        self.st.update(init())
        self.assertEqual(self.st.status, P.RUNNING)
        self.assertEqual(self.st.session_id, "s1")
        self.assertEqual(self.st.model, "m")
        self.assertEqual(self.st.cwd, "C:/ws")
        self.assertEqual(self.st.permission_mode, "acceptEdits")

    def test_tool_use_recorded(self) -> None:
        self.st.update(init())
        self.st.update({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write"},
        ]}})
        self.assertEqual(self.st.tool_calls, ["Write"])
        self.assertEqual(self.st.last_activity, "tool:Write")
        self.assertEqual(self.st.turns_seen, 1)

    def test_result_completes(self) -> None:
        self.st.update(init())
        self.st.update(result())
        self.assertEqual(self.st.status, P.COMPLETED)
        self.assertEqual(self.st.answer, "答案")
        self.assertEqual(self.st.num_turns, 2)
        self.assertIsNotNone(self.st.ended_at)

    def test_is_error_fails(self) -> None:
        self.st.update(init())
        self.st.update(result(is_error=True, subtype="error", result="炸了"))
        self.assertEqual(self.st.status, P.FAILED)

    def test_permission_denials_captured(self) -> None:
        """README 结论 3：被拒工具不会挂起，但 status 仍是 completed。

        这条锁住的是那个坑本身 —— 汇总层不能只看 status。
        """
        self.st.update(init())
        self.st.update(result(permission_denials=[{"tool_name": "Bash"}]))
        self.assertEqual(self.st.status, P.COMPLETED)
        self.assertEqual(len(self.st.permission_denials), 1)
        doc = self.st.result_document(0, "x.jsonl")
        self.assertEqual(len(doc["diagnostics"]["permission_denials"]), 1)

    def test_malformed_content_does_not_crash(self) -> None:
        self.st.update(init())
        for bad in (
            {"type": "assistant"},
            {"type": "assistant", "message": None},
            {"type": "assistant", "message": {"content": "不是列表"}},
            {"type": "assistant", "message": {"content": ["不是字典"]}},
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            {"type": "没见过的类型"},
            {},
        ):
            self.st.update(bad)
        self.assertNotIn(self.st.status, P.TERMINAL)


class TestMark(unittest.TestCase):
    def test_mark_sets_terminal_and_note(self) -> None:
        st = AgentState("a1", "r1")
        st.update(init())
        st.mark(P.TIMEOUT, "超过 8s 被终止")
        self.assertEqual(st.status, P.TIMEOUT)
        self.assertEqual(st.last_activity, "超过 8s 被终止")
        self.assertIsNotNone(st.ended_at)

    def test_mark_preserves_existing_ended_at(self) -> None:
        st = AgentState("a1", "r1")
        st.update(init())
        st.update(result())
        first = st.ended_at
        st.mark(P.CANCELLED)
        self.assertEqual(st.ended_at, first)


class TestDocuments(unittest.TestCase):
    def test_snapshot_is_json_safe_and_small(self) -> None:
        st = AgentState("a1", "r1")
        st.update(init())
        snap = st.snapshot()
        self.assertEqual(snap["agent_id"], "a1")
        self.assertEqual(snap["status"], P.RUNNING)
        # tool_calls 在快照里是计数不是列表：这个文件高频写，必须廉价
        self.assertIsInstance(snap["tool_calls"], int)

    def test_result_document_shape(self) -> None:
        st = AgentState("a1", "r1")
        st.update(init())
        st.update(result())
        doc = st.result_document(0, "e.jsonl")
        for key in ("agent_id", "run_id", "status", "session_id", "answer",
                    "exit_code", "metrics", "diagnostics", "events_path"):
            self.assertIn(key, doc)
        self.assertEqual(doc["metrics"]["total_cost_usd"], 0.01)
        self.assertEqual(doc["exit_code"], 0)
