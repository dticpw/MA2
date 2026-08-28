"""汇总层。纯字符串与纯结构，不碰 IO，不起进程。

这里锁住的是两条命题：简报必须**完整承载**汇总 Agent 所需的输入（否则它只能
瞎编），以及 final.md 的价值**不依赖**综合层成功。
"""

from __future__ import annotations

import unittest

from ma2 import aggregate as AG


def record(agent_id="a0", *, ok=True, reasons=None, answer="我的回答",
           status="completed", attempt=1, branch=None, artifacts=None,
           prompt="去做某件事", **extra):
    rec = {
        "agent_id": agent_id, "prompt": prompt, "status": status,
        "ok": ok, "reasons": reasons or [], "attempt": attempt,
        "answer": answer, "answer_clipped": False,
        "branch": branch, "cost_usd": 0.5, "cost_is_complete": True,
        "duration_ms": 1500, "artifacts": artifacts,
    }
    rec.update(extra)
    return rec


def artifacts(branch="ma2/r1/a0", files=None, total=None, omitted=0):
    files = files if files is not None else [
        {"path": "NOTES.md", "change": "A", "text": "分支上的内容", "truncated": False}]
    return {"branch": branch, "base": "abc123", "files": files,
            "total_files": total if total is not None else len(files),
            "omitted_files": omitted}


class TestClip(unittest.TestCase):
    def test_short_text_is_untouched(self) -> None:
        self.assertEqual(AG.clip("abc", 10), ("abc", False))

    def test_long_text_is_cut_and_flagged(self) -> None:
        self.assertEqual(AG.clip("abcdef", 3), ("abc", True))

    def test_none_is_treated_as_empty(self) -> None:
        self.assertEqual(AG.clip(None, 10), ("", False))


class TestGather(unittest.TestCase):
    """gather 只在 worktree 模式下读分支；其余情况必须安静地降级。"""

    def test_maps_verdict_and_metrics(self) -> None:
        recs = AG.gather([{
            "agent_id": "a0", "status": "completed", "answer": "答",
            "verdict": {"ok": True, "reasons": []}, "attempt": 2,
            "metrics": {"cost_all_attempts_usd": 1.25, "cost_is_complete": False,
                        "wall_ms_all_attempts": 9000},
            "workspace": {"kind": "plain"},
        }])
        rec = recs[0]
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["attempt"], 2)
        self.assertEqual(rec["cost_usd"], 1.25)
        self.assertFalse(rec["cost_is_complete"])
        self.assertEqual(rec["duration_ms"], 9000)
        self.assertIsNone(rec["artifacts"], "plain 工作区没有分支可读")

    def test_prompt_is_taken_from_the_task(self) -> None:
        """result.json 不带 prompt。不把它补进简报，汇总 Agent 就不知道
        每个 Agent 原本被要求做什么，只能凭回答倒推。"""
        recs = AG.gather([{"agent_id": "a0", "verdict": {"ok": True}}],
                         [{"id": "a0", "prompt": "去查 X"}])
        self.assertEqual(recs[0]["prompt"], "去查 X")

    def test_failure_reasons_survive(self) -> None:
        recs = AG.gather([{"agent_id": "a0", "status": "completed",
                           "verdict": {"ok": False,
                                       "reasons": ["permission_denied"]}}])
        self.assertFalse(recs[0]["ok"])
        self.assertEqual(recs[0]["reasons"], ["permission_denied"])

    def test_long_answer_is_clipped_and_flagged(self) -> None:
        recs = AG.gather([{"agent_id": "a0", "answer": "字" * (AG.ANSWER_LIMIT + 10),
                           "verdict": {"ok": True}}])
        self.assertEqual(len(recs[0]["answer"]), AG.ANSWER_LIMIT)
        self.assertTrue(recs[0]["answer_clipped"])


class TestBuildBrief(unittest.TestCase):
    def test_carries_answer_prompt_and_branch_content(self) -> None:
        """简报是汇总 Agent 看到的**全部**输入。三样缺一样它就得靠猜。"""
        brief = AG.build_brief("r1", "计划", [
            record("a0", prompt="去查 X", answer="X 是 42",
                   branch="ma2/r1/a0", artifacts=artifacts()),
        ])
        self.assertIn("去查 X", brief)
        self.assertIn("X 是 42", brief)
        self.assertIn("分支上的内容", brief)
        self.assertIn("ma2/r1/a0", brief)

    def test_failed_agents_are_included_with_their_reason(self) -> None:
        """失败的 Agent 不能从简报里消失 —— 汇总要能说出缺口在哪，
        而不是把 N-1 份回答说成全部。"""
        brief = AG.build_brief("r1", None, [
            record("a0"), record("a1", ok=False, reasons=["timeout"],
                                 status="timeout", answer=""),
        ])
        self.assertIn("a1", brief)
        self.assertIn("FAIL(timeout)", brief)
        self.assertIn("1 个", brief)

    def test_truncation_is_announced_not_silent(self) -> None:
        brief = AG.build_brief("r1", None, [
            record(artifacts=artifacts(total=30, omitted=29)),
        ])
        self.assertIn("29", brief)
        self.assertIn("未列出", brief)

    def test_binary_files_contribute_no_content(self) -> None:
        brief = AG.build_brief("r1", None, [
            record(artifacts=artifacts(files=[
                {"path": "a.png", "change": "A", "binary": True}])),
        ])
        self.assertIn("a.png", brief)
        self.assertIn("二进制", brief)

    def test_branch_read_failure_is_reported(self) -> None:
        brief = AG.build_brief("r1", None, [
            record(artifacts={"branch": "b", "error": "没有那个分支", "files": []}),
        ])
        self.assertIn("没有那个分支", brief)

    def test_empty_answer_is_marked_not_blank(self) -> None:
        brief = AG.build_brief("r1", None, [record(answer="")])
        self.assertIn("(空)", brief)

    def test_no_records_still_produces_a_document(self) -> None:
        brief = AG.build_brief("r1", "空计划", [])
        self.assertIn("r1", brief)


class TestAggregatorTask(unittest.TestCase):
    def test_brief_is_carried_in_the_prompt(self) -> None:
        """简报整份走 stdin，汇总 Agent 因此不需要任何工具就能读全输入。"""
        task = AG.aggregator_task({}, "简报正文在此")
        self.assertIn("简报正文在此", task["prompt"])
        self.assertEqual(task["allowed_tools"], [])

    def test_defaults_are_conservative(self) -> None:
        task = AG.aggregator_task({}, "x")
        self.assertEqual(task["id"], "aggregator")
        self.assertEqual(task["retries"], 1)
        self.assertFalse(task["checks"]["require_changes"],
                         "汇总不写文件，require_changes 必须是关的")

    def test_plan_can_override_everything(self) -> None:
        task = AG.aggregator_task({"aggregator": {
            "id": "sum", "prompt": "自定义指令", "model": "opus",
            "timeout_sec": 60, "retries": 0,
        }}, "简报")
        self.assertEqual(task["id"], "sum")
        self.assertTrue(task["prompt"].startswith("自定义指令"))
        self.assertEqual(task["model"], "opus")
        self.assertEqual(task["timeout_sec"], 60)
        self.assertEqual(task["retries"], 0)

    def test_absent_keys_are_not_injected_as_none(self) -> None:
        """model=None 会被 build_argv 当成"没给"，但 launcher=None 更危险 ——
        显式的 None 和"没这个键"在下游不是一回事，干脆不写进去。"""
        task = AG.aggregator_task({}, "x")
        self.assertNotIn("model", task)
        self.assertNotIn("launcher", task)


class TestRenderFinal(unittest.TestCase):
    SUMMARY = {"finished_at": "2026-08-28T00:00:00Z", "completed": 2}

    def test_synthesis_is_placed_in_the_body(self) -> None:
        out = AG.render_final("r1", "计划", self.SUMMARY, [record()],
                              {"ok": True, "answer": "综合结论在此"})
        self.assertIn("综合结论在此", out)

    def test_document_is_complete_without_a_synthesis(self) -> None:
        """没开综合层，机械汇总照样是一份完整交付物。"""
        out = AG.render_final("r1", "计划", self.SUMMARY,
                              [record(branch="ma2/r1/a0", artifacts=artifacts())],
                              None)
        self.assertIn("## 明细", out)
        self.assertIn("我的回答", out)
        self.assertIn("ma2/r1/a0", out)
        self.assertIn("未启用综合层", out)

    def test_failed_synthesis_does_not_destroy_the_document(self) -> None:
        """核心命题：总结器挂了，这次 run 的成果不能跟着一起没。"""
        out = AG.render_final("r1", "计划", self.SUMMARY,
                              [record(branch="ma2/r1/a0", artifacts=artifacts())],
                              {"ok": False, "reasons": ["timeout"],
                               "status": "timeout", "attempt": 2})
        self.assertIn("综合层失败", out)
        self.assertIn("timeout", out)
        self.assertIn("我的回答", out, "各 Agent 的回答必须还在")
        self.assertIn("ma2/r1/a0", out, "产出位置必须还在")

    def test_failed_agents_appear_in_the_table_with_reasons(self) -> None:
        out = AG.render_final("r1", None, self.SUMMARY, [
            record("a0"), record("a1", ok=False, reasons=["permission_denied"]),
        ], None)
        self.assertIn("FAIL(permission_denied)", out)
        self.assertIn("1/2", out)

    def test_incomplete_cost_is_marked(self) -> None:
        out = AG.render_final("r1", None, self.SUMMARY,
                              [record(cost_is_complete=False)], None)
        self.assertIn("≥$", out)

    def test_artifact_locations_point_at_branches(self) -> None:
        """结论 7：工作目录会被回收，能长期引用的只有分支。"""
        out = AG.render_final("r1", None, self.SUMMARY,
                              [record(branch="ma2/r1/a0", artifacts=artifacts())],
                              None)
        self.assertIn("## 产出在哪", out)
        self.assertIn("`ma2/r1/a0`", out)
        self.assertNotIn("workspaces", out)

    def test_no_artifact_section_when_nothing_was_produced(self) -> None:
        out = AG.render_final("r1", None, self.SUMMARY, [record()], None)
        self.assertNotIn("## 产出在哪", out)


if __name__ == "__main__":
    unittest.main()
