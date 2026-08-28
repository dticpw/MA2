"""成败判定。纯函数，一条测试锁一条策略。

这里的核心命题是 README 结论 3 的反面：`status == completed` 不等于成功。
"""

from __future__ import annotations

import unittest

from ma2 import policy as PO
from ma2 import protocol as P


def result(status=P.COMPLETED, *, answer="做完了", denials=None):
    return {
        "status": status,
        "answer": answer,
        "diagnostics": {"permission_denials": denials or []},
    }


class TestEvaluate(unittest.TestCase):
    def test_clean_completion_is_ok(self) -> None:
        v = PO.evaluate(result())
        self.assertTrue(v.ok)
        self.assertEqual(v.reasons, [])

    def test_permission_denial_is_not_success(self) -> None:
        """结论 3 本体：Agent 正常收尾了，但它什么也没干成。"""
        v = PO.evaluate(result(denials=[{"tool_name": "Bash"}]))
        self.assertFalse(v.ok)
        self.assertIn(PO.PERMISSION_DENIED, v.reasons)

    def test_permission_denial_is_never_retried(self) -> None:
        """重试解决不了配置错误，只会把同一笔钱烧两遍。"""
        v = PO.evaluate(result(denials=[{"tool_name": "Bash"}]))
        self.assertFalse(v.retryable)

    def test_denial_check_can_be_turned_off(self) -> None:
        """有些任务本来就预期被拦（比如探测权限边界）。"""
        v = PO.evaluate(result(denials=[{"tool_name": "Bash"}]),
                        checks={**PO.DEFAULT_CHECKS, "deny_permission_denials": False})
        self.assertTrue(v.ok)

    def test_timeout_is_a_retryable_failure(self) -> None:
        v = PO.evaluate(result(P.TIMEOUT, answer=None))
        self.assertEqual(v.reasons, [PO.TIMEOUT])
        self.assertTrue(v.retryable)

    def test_failed_status_is_retryable(self) -> None:
        v = PO.evaluate(result(P.FAILED, answer=None))
        self.assertEqual(v.reasons, [PO.CRASHED])
        self.assertTrue(v.retryable)

    def test_empty_answer_is_a_failure(self) -> None:
        v = PO.evaluate(result(answer="   "))
        self.assertEqual(v.reasons, [PO.EMPTY_ANSWER])
        self.assertTrue(v.retryable)

    def test_empty_answer_check_can_be_turned_off(self) -> None:
        v = PO.evaluate(result(answer=""),
                        checks={**PO.DEFAULT_CHECKS, "require_answer": False})
        self.assertTrue(v.ok)

    def test_empty_answer_not_reported_on_top_of_a_crash(self) -> None:
        """崩了当然没有回答。多报一条只会掩盖真正的原因。"""
        v = PO.evaluate(result(P.FAILED, answer=None))
        self.assertNotIn(PO.EMPTY_ANSWER, v.reasons)

    def test_require_changes_off_by_default(self) -> None:
        self.assertTrue(PO.evaluate(result(), changes={"dirty_files": 0}).ok)

    def test_require_changes_catches_a_no_op_agent(self) -> None:
        checks = {**PO.DEFAULT_CHECKS, "require_changes": True}
        v = PO.evaluate(result(), checks=checks,
                        changes={"dirty_files": 0, "committed": False})
        self.assertEqual(v.reasons, [PO.NO_CHANGES])

    def test_require_changes_accepts_dirty_or_committed(self) -> None:
        checks = {**PO.DEFAULT_CHECKS, "require_changes": True}
        self.assertTrue(PO.evaluate(result(), checks=checks,
                                    changes={"dirty_files": 2}).ok)
        self.assertTrue(PO.evaluate(result(), checks=checks,
                                    changes={"dirty_files": 0,
                                             "committed": True}).ok)

    def test_mixed_reasons_are_all_reported(self) -> None:
        v = PO.evaluate(result(answer="", denials=[{"tool_name": "Write"}]))
        self.assertEqual(set(v.reasons), {PO.PERMISSION_DENIED, PO.EMPTY_ANSWER})

    def test_one_unretryable_reason_blocks_the_whole_retry(self) -> None:
        """有一条重试也解决不了，就没必要为另一条重试。"""
        v = PO.evaluate(result(answer="", denials=[{"tool_name": "Write"}]))
        self.assertFalse(v.retryable)


class TestResolveChecks(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(PO.resolve_checks({}), PO.DEFAULT_CHECKS)

    def test_run_level_overrides_default(self) -> None:
        checks = PO.resolve_checks({}, {"checks": {"require_answer": False}})
        self.assertFalse(checks["require_answer"])
        self.assertTrue(checks["deny_permission_denials"])

    def test_task_level_beats_run_level(self) -> None:
        checks = PO.resolve_checks({"checks": {"require_answer": True}},
                                   {"checks": {"require_answer": False}})
        self.assertTrue(checks["require_answer"])

    def test_resolve_does_not_mutate_the_shared_default(self) -> None:
        PO.resolve_checks({"checks": {"require_changes": True}})
        self.assertFalse(PO.DEFAULT_CHECKS["require_changes"])


class TestRetryPlan(unittest.TestCase):
    def test_no_retries_by_default(self) -> None:
        self.assertEqual(PO.retry_plan({})[0], 1, "默认不重试")

    def test_retries_is_extra_attempts_not_total(self) -> None:
        """retries=2 意思是最多跑 3 次，不是 2 次。"""
        self.assertEqual(PO.retry_plan({"retries": 2})[0], 3)

    def test_task_beats_run_level(self) -> None:
        self.assertEqual(PO.retry_plan({"retries": 1}, {"retries": 5})[0], 2)

    def test_negative_retries_still_runs_once(self) -> None:
        self.assertEqual(PO.retry_plan({"retries": -9})[0], 1)

    def test_delay_and_reset_defaults(self) -> None:
        _, delay, reset = PO.retry_plan({})
        self.assertEqual(delay, 2.0)
        self.assertTrue(reset, "重试默认从干净工作区起步")


if __name__ == "__main__":
    unittest.main()
