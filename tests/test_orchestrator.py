"""三阶段编排。这里锁住的是并行、隔离与收尾这三条主线。"""

from __future__ import annotations

import unittest
from unittest import mock

from ma2 import protocol as P
from ma2 import workspace as W
from ma2.orchestrator import Orchestrator

from .support import TempDirCase, fake_task, git


class OrchestratorCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths = P.RunPaths(self.tmp / "runs", "r1")
        self.paths.ensure()

    def build(self, plan, **kw) -> Orchestrator:
        kw.setdefault("quiet", True)
        return Orchestrator(plan, self.paths, **kw)

    def worktree_plan(self, repo, tasks, **extra):
        plan = {
            "run_name": "t",
            "workspace_defaults": {
                "kind": W.WORKTREE, "repo": str(repo),
                "base_ref": "main", "branch_prefix": "ma2",
            },
            "tasks": tasks,
        }
        plan.update(extra)
        return plan


class TestIsolation(OrchestratorCase):
    """核心命题：N 个 Agent 写同一个文件名，彼此不得覆盖。

    这是 plans/parallel3.json 那次 $0.9 / 17s 的实测，用假 Agent 变成免费的。
    同名冲突是刻意设计的 —— 隔离一旦失效，它们必然互相覆盖。
    """

    N = 3

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()
        self.tasks = [
            fake_task(f"a{i}", scenario="write",
                      write_file="NOTES.md", write_text=f"内容-{i}",
                      answer=f"答案-{i}")
            for i in range(self.N)
        ]

    def test_each_agent_keeps_its_own_file(self) -> None:
        orch = self.build(self.worktree_plan(self.repo, self.tasks))
        summary = orch.execute()

        self.assertEqual(summary["status"], P.COMPLETED)
        self.assertEqual(summary["completed"], self.N)
        for i in range(self.N):
            ws = orch.workspaces[f"a{i}"]
            self.assertEqual((ws.path / "NOTES.md").read_text(encoding="utf-8"),
                             f"内容-{i}")

    def test_source_repo_is_untouched(self) -> None:
        orch = self.build(self.worktree_plan(self.repo, self.tasks))
        orch.execute()
        self.assertFalse((self.repo / "NOTES.md").exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_each_agent_gets_its_own_branch(self) -> None:
        orch = self.build(self.worktree_plan(self.repo, self.tasks))
        orch.execute()
        branches = git(self.repo, "branch", "--list", "ma2/*").split()
        for i in range(self.N):
            self.assertIn(f"ma2/r1/a{i}", branches)

    def test_work_survives_cleanup_on_its_branch(self) -> None:
        """结论 7 的端到端回归：回收工作区后，产出仍在分支上。"""
        orch = self.build(self.worktree_plan(self.repo, self.tasks), cleanup=True)
        orch.execute()
        for i in range(self.N):
            self.assertFalse(orch.workspaces[f"a{i}"].path.exists())
            self.assertEqual(git(self.repo, "show", f"ma2/r1/a{i}:NOTES.md"),
                             f"内容-{i}")


class TestParallelism(OrchestratorCase):
    def test_max_parallel_is_respected(self) -> None:
        """并发上限必须真的限流，否则 N 大了会把机器打满。"""
        plan = {"tasks": [fake_task(f"a{i}") for i in range(6)], "max_parallel": 2}
        orch = self.build(plan)

        import threading
        live = 0
        peak = 0
        lock = threading.Lock()

        from ma2 import runner as R
        original = R.run_agent

        def counting(*args, **kwargs):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            try:
                return original(*args, **kwargs)
            finally:
                with lock:
                    live -= 1

        with mock.patch("ma2.orchestrator.run_agent", counting):
            orch.execute()
        self.assertLessEqual(peak, 2, f"实际并发峰值 {peak}")
        self.assertGreater(peak, 1, "根本没并行起来")

    def test_plan_max_parallel_beats_argument(self) -> None:
        plan = {"tasks": [fake_task("a0")], "max_parallel": 7}
        self.assertEqual(self.build(plan, max_parallel=2).max_parallel, 7)

    def test_max_parallel_floor_is_one(self) -> None:
        plan = {"tasks": [fake_task("a0")], "max_parallel": 0}
        self.assertEqual(self.build(plan).max_parallel, 1)


class TestAggregatedStatus(OrchestratorCase):
    def test_run_status_json_covers_every_agent(self) -> None:
        """观察面只读这一个文件，所以它必须是完整的。"""
        plan = {"tasks": [fake_task(f"a{i}") for i in range(3)]}
        orch = self.build(plan)
        orch.execute()

        doc = P.read_json(self.paths.run_status)
        self.assertEqual(doc["run_id"], "r1")
        self.assertEqual({a["agent_id"] for a in doc["agents"]}, {"a0", "a1", "a2"})
        for a in doc["agents"]:
            self.assertEqual(a["status"], P.COMPLETED)

    def test_status_written_concurrently_stays_valid_json(self) -> None:
        """多线程共同刷新同一个文件，原子写必须让它始终可解析。"""
        plan = {"tasks": [fake_task(f"a{i}") for i in range(6)], "max_parallel": 6}
        orch = self.build(plan)
        orch.execute()
        self.assertIsNotNone(P.read_json(self.paths.run_status))


class TestFailureHandling(OrchestratorCase):
    def test_one_failure_does_not_block_the_others(self) -> None:
        plan = {"tasks": [
            fake_task("ok1"),
            fake_task("bad", scenario="crash"),
            fake_task("ok2"),
        ]}
        orch = self.build(plan)
        summary = orch.execute()
        self.assertEqual(summary["status"], P.FAILED)
        self.assertEqual(summary["completed"], 2)
        by_id = {a["agent_id"]: a["status"] for a in summary["agents"]}
        self.assertEqual(by_id, {"ok1": P.COMPLETED, "bad": P.FAILED,
                                 "ok2": P.COMPLETED})

    def test_executor_exception_is_contained(self) -> None:
        """单个 Agent 抛异常不能拖垮整个 run。"""
        plan = {"tasks": [fake_task("ok1"), fake_task("boom")]}
        orch = self.build(plan)
        from ma2 import runner as R
        original = R.run_agent

        def flaky(task, *args, **kwargs):
            if task["id"] == "boom":
                raise RuntimeError("模拟执行器崩溃")
            return original(task, *args, **kwargs)

        with mock.patch("ma2.orchestrator.run_agent", flaky):
            summary = orch.execute()
        by_id = {a["agent_id"]: a["status"] for a in summary["agents"]}
        self.assertEqual(by_id["boom"], P.FAILED)
        self.assertEqual(by_id["ok1"], P.COMPLETED)

    def test_prepare_failure_rolls_back_created_worktrees(self) -> None:
        """第二个工作区建失败时，第一个必须被回收，不留半拉子现场。"""
        repo = self.make_repo()
        tasks = [fake_task("a0"), fake_task("a1"), fake_task("a2")]
        plan = self.worktree_plan(repo, tasks)
        orch = self.build(plan)

        original = W.prepare
        calls = {"n": 0}

        def failing(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise W.WorkspaceError("模拟 worktree 创建失败")
            return original(*args, **kwargs)

        with mock.patch("ma2.orchestrator.W.prepare", failing):
            with self.assertRaises(W.WorkspaceError):
                orch.execute()

        self.assertEqual(git(repo, "branch", "--list", "ma2/*"), "",
                         "回滚必须连分支一起删掉")
        self.assertEqual(orch.workspaces, {})


class TestCollectAndCommit(OrchestratorCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()
        self.tasks = [fake_task("a0", scenario="write",
                                write_file="NOTES.md", write_text="产出")]

    def test_autocommit_on_by_default(self) -> None:
        orch = self.build(self.worktree_plan(self.repo, self.tasks))
        orch.execute()
        doc = P.read_json(self.paths.result("a0"))
        self.assertTrue(doc["workspace"]["committed"])
        self.assertEqual(doc["workspace"]["dirty_files"], 0)
        self.assertTrue(doc["workspace"]["autocommit"]["committed"])

    def test_no_commit_leaves_work_uncommitted(self) -> None:
        orch = self.build(self.worktree_plan(self.repo, self.tasks),
                          autocommit=False)
        orch.execute()
        doc = P.read_json(self.paths.result("a0"))
        self.assertFalse(doc["workspace"]["committed"])
        self.assertEqual(doc["workspace"]["dirty_files"], 1)

    def test_result_records_repo_for_later_prune(self) -> None:
        orch = self.build(self.worktree_plan(self.repo, self.tasks))
        orch.execute()
        doc = P.read_json(self.paths.result("a0"))
        self.assertEqual(doc["workspace"]["repo"], str(self.repo.resolve()))

    def test_run_json_carries_the_summary(self) -> None:
        orch = self.build(self.worktree_plan(self.repo, self.tasks))
        summary = orch.execute()
        on_disk = P.read_json(self.paths.run_json)
        self.assertEqual(on_disk["status"], summary["status"])
        self.assertEqual(on_disk["completed"], 1)


class TestRetry(OrchestratorCase):
    """重试。夹具是 flaky：前 N 次失败、之后成功。

    只断言"重试发生了"是不够的 —— 必须断言第二次跑出了**不同的结果**，
    否则一个什么都不做的 retry 循环也能让测试通过。
    """

    def flaky(self, agent_id="a0", *, fail_times=1, fail_as="crash", **extra):
        return fake_task(
            agent_id, scenario="flaky",
            counter_file=str(self.tmp / f"{agent_id}.count"),
            fail_times=fail_times, fail_as=fail_as,
            write_file="NOTES.md", write_text="产出", answer="终于好了",
            **extra,
        )

    def test_retry_turns_a_transient_failure_into_success(self) -> None:
        plan = {"tasks": [self.flaky(retries=2, retry_delay_sec=0)]}
        orch = self.build(plan)
        summary = orch.execute()
        self.assertEqual(summary["status"], P.COMPLETED)
        doc = P.read_json(self.paths.result("a0"))
        self.assertEqual(doc["attempt"], 2, "应当在第 2 次尝试成功")
        self.assertEqual(doc["answer"], "终于好了")
        self.assertTrue(doc["verdict"]["ok"])

    def test_without_retries_the_same_task_fails(self) -> None:
        """反向对照：证明上一条的成功来自重试，不是夹具本来就会成功。"""
        plan = {"tasks": [self.flaky()]}
        summary = self.build(plan).execute()
        self.assertEqual(summary["status"], P.FAILED)
        self.assertEqual(P.read_json(self.paths.result("a0"))["attempt"], 1)

    def test_attempts_are_recorded_one_by_one(self) -> None:
        plan = {"tasks": [self.flaky(retries=2, retry_delay_sec=0)]}
        self.build(plan).execute()
        attempts = P.read_json(self.paths.result("a0"))["attempts"]
        self.assertEqual([a["attempt"] for a in attempts], [1, 2])
        self.assertEqual(attempts[0]["status"], P.FAILED)
        self.assertEqual(attempts[1]["status"], P.COMPLETED)

    def test_each_attempt_keeps_its_own_event_stream(self) -> None:
        """失败那次的审计流最需要留证，不能被重试覆盖掉（§13）。"""
        plan = {"tasks": [self.flaky(retries=2, retry_delay_sec=0)]}
        self.build(plan).execute()
        first = self.paths.events("a0", 1)
        second = self.paths.events("a0", 2)
        self.assertTrue(first.exists() and second.exists())
        self.assertIn("炸了", first.read_text(encoding="utf-8"))
        self.assertIn("终于好了", second.read_text(encoding="utf-8"))

    def test_retries_are_exhausted_and_reported(self) -> None:
        plan = {"tasks": [self.flaky(fail_times=9, retries=2, retry_delay_sec=0)]}
        summary = self.build(plan).execute()
        self.assertEqual(summary["status"], P.FAILED)
        doc = P.read_json(self.paths.result("a0"))
        self.assertEqual(doc["attempt"], 3, "retries=2 意味着最多跑 3 次")
        self.assertFalse(doc["verdict"]["ok"])

    def test_timeout_is_retried(self) -> None:
        plan = {"tasks": [self.flaky(fail_as="hang", hang_sec=30,
                                     timeout_sec=2, retries=1, retry_delay_sec=0)]}
        summary = self.build(plan).execute()
        self.assertEqual(summary["status"], P.COMPLETED)
        attempts = P.read_json(self.paths.result("a0"))["attempts"]
        self.assertEqual(attempts[0]["status"], P.TIMEOUT)

    def test_permission_denial_is_never_retried(self) -> None:
        """给了 3 次重试也只跑 1 次：重试解决不了配置错误，只会重复烧钱。"""
        plan = {"tasks": [fake_task("a0", scenario="denial", retries=3,
                                    retry_delay_sec=0)]}
        summary = self.build(plan).execute()
        doc = P.read_json(self.paths.result("a0"))
        self.assertEqual(doc["attempt"], 1)
        self.assertEqual(doc["status"], P.COMPLETED, "协议层事实不变")
        self.assertFalse(doc["verdict"]["ok"], "但判定层认定失败")
        self.assertEqual(summary["status"], P.FAILED)

    def test_cost_of_every_attempt_is_summed(self) -> None:
        """只报最后一次的费用等于瞒账。"""
        plan = {"tasks": [self.flaky(retries=2, retry_delay_sec=0)]}
        self.build(plan).execute()
        doc = P.read_json(self.paths.result("a0"))
        self.assertIn("cost_all_attempts_usd", doc["metrics"])
        self.assertEqual(len(doc["attempts"]), 2)

    def test_retry_delay_is_actually_waited(self) -> None:
        import time as _t
        plan = {"tasks": [self.flaky(retries=1, retry_delay_sec=0.6)]}
        t0 = _t.monotonic()
        self.build(plan).execute()
        self.assertGreaterEqual(_t.monotonic() - t0, 0.6, "退避没有真的执行")


class TestRetryWorkspaceReset(OrchestratorCase):
    """重试前要不要清掉上次的半成品。

    夹具让每次尝试写 `<n>-NOTES.md`，于是"重置有没有发生"可以被直接观察，
    而不是靠断言某个函数被调用过。
    """

    def flaky(self, **extra):
        return fake_task(
            "a0", scenario="flaky", counter_file=str(self.tmp / "c"),
            fail_times=1, fail_as="write-crash",
            write_file="NOTES.md", write_text="产出", answer="好了",
            retries=1, retry_delay_sec=0, **extra,
        )

    def test_reset_discards_the_failed_attempts_leftovers(self) -> None:
        orch = self.build({"tasks": [self.flaky()]})
        orch.execute()
        ws = orch.workspaces["a0"].path
        self.assertFalse((ws / "1-NOTES.md").exists(), "上次的半成品应被清掉")
        self.assertTrue((ws / "2-NOTES.md").exists())

    def test_reset_can_be_turned_off(self) -> None:
        orch = self.build({"tasks": [self.flaky(reset_between_attempts=False)]})
        orch.execute()
        ws = orch.workspaces["a0"].path
        self.assertTrue((ws / "1-NOTES.md").exists(), "关掉后残留应当保留")
        self.assertTrue((ws / "2-NOTES.md").exists())

    def test_reset_works_in_a_worktree(self) -> None:
        repo = self.make_repo()
        plan = {
            "run_name": "t",
            "workspace_defaults": {"kind": W.WORKTREE, "repo": str(repo),
                                   "base_ref": "main", "branch_prefix": "ma2"},
            "tasks": [self.flaky()],
        }
        orch = self.build(plan)
        summary = orch.execute()
        self.assertEqual(summary["status"], P.COMPLETED)
        ws = orch.workspaces["a0"].path
        self.assertFalse((ws / "1-NOTES.md").exists())
        # 只有第二次的产出被提交上去
        self.assertEqual(git(repo, "show", "ma2/r1/a0:2-NOTES.md"), "产出")


class TestReportedDuration(OrchestratorCase):
    """账面不能比现实好看。

    `duration_ms` 和 `total_cost_usd` 都来自 result 事件，而超时的运行**根本
    收不到那个事件** —— 于是一次真花了钱、真占了几十秒的失败，在汇总表里
    显示成 0.0s / $0.0000。墙钟是编排器自己就能测的，至少让耗时不说谎。
    """

    def test_timed_out_run_still_reports_wall_clock(self) -> None:
        plan = {"tasks": [fake_task("a0", scenario="hang", hang_sec=30,
                                    timeout_sec=2)]}
        summary = self.build(plan).execute()
        doc = P.read_json(self.paths.result("a0"))
        self.assertEqual(doc["status"], P.TIMEOUT)
        self.assertIsNone(doc["metrics"].get("duration_ms"),
                          "前提：超时的运行确实没有 result 事件里的耗时")
        self.assertGreaterEqual(doc["metrics"]["wall_ms"], 1500,
                                "墙钟应当反映真的等了 2 秒")
        self.assertGreaterEqual(summary["agents"][0]["duration_ms"], 1500,
                                "汇总不能把这次失败报成 0 秒")

    def test_duration_covers_every_attempt(self) -> None:
        """和 cost 一个口径：重试过的运行只报最后一次也是在瞒账。"""
        plan = {"tasks": [fake_task("a0", scenario="flaky",
                                    counter_file=str(self.tmp / "a0.count"),
                                    fail_as="hang", hang_sec=30, timeout_sec=2,
                                    answer="终于好了",
                                    retries=1, retry_delay_sec=0)]}
        summary = self.build(plan).execute()
        doc = P.read_json(self.paths.result("a0"))
        self.assertEqual(doc["attempt"], 2)
        self.assertGreaterEqual(doc["metrics"]["wall_ms_all_attempts"],
                                doc["metrics"]["wall_ms"],
                                "总耗时不能小于最后一次的耗时")
        self.assertGreaterEqual(summary["agents"][0]["duration_ms"], 1500,
                                "被超时吃掉的那 2 秒不能从账上消失")


    def test_unknown_cost_is_marked_as_a_lower_bound(self) -> None:
        """耗时能靠墙钟自测，费用不能。超时那次花了多少钱无从得知，
        按 0 计入之后必须标明这是下界 —— 否则 $0.0000 看起来像"这次失败免费"。
        """
        plan = {"tasks": [fake_task("a0", scenario="hang", hang_sec=30,
                                    timeout_sec=2),
                          fake_task("a1", scenario="ok")]}
        summary = self.build(plan).execute()
        self.assertFalse(summary["agents"][0]["cost_is_complete"])
        self.assertTrue(summary["agents"][1]["cost_is_complete"])
        self.assertFalse(summary["cost_is_complete"], "有一个未知就整体未知")

    def test_cost_is_complete_when_every_attempt_reported(self) -> None:
        plan = {"tasks": [fake_task("a0", scenario="ok")]}
        summary = self.build(plan).execute()
        self.assertTrue(summary["cost_is_complete"])


class TestVerdict(OrchestratorCase):
    """汇总口径按 verdict 而不是 status —— 结论 3 的直接后果。"""

    def test_denial_run_is_not_counted_as_success(self) -> None:
        plan = {"tasks": [fake_task("a0", scenario="denial")]}
        summary = self.build(plan).execute()
        self.assertEqual(summary["ok"], 0)
        self.assertEqual(summary["completed"], 1, "协议层确实 completed")
        self.assertEqual(summary["status"], P.FAILED)
        self.assertEqual(summary["agents"][0]["reasons"], ["permission_denied"])

    def test_empty_answer_run_is_not_counted_as_success(self) -> None:
        plan = {"tasks": [fake_task("a0", scenario="empty")]}
        summary = self.build(plan).execute()
        self.assertEqual(summary["ok"], 0)
        self.assertEqual(summary["agents"][0]["reasons"], ["empty_answer"])

    def test_checks_can_be_relaxed_at_run_level(self) -> None:
        plan = {"tasks": [fake_task("a0", scenario="denial")],
                "checks": {"deny_permission_denials": False}}
        summary = self.build(plan).execute()
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["status"], P.COMPLETED)

    def test_require_changes_flags_an_agent_that_did_nothing(self) -> None:
        repo = self.make_repo()
        plan = self.worktree_plan(repo, [fake_task("a0", scenario="ok")],
                                  checks={"require_changes": True})
        summary = self.build(plan).execute()
        self.assertEqual(summary["agents"][0]["reasons"], ["no_changes"])

    def test_require_changes_passes_when_files_were_written(self) -> None:
        repo = self.make_repo()
        plan = self.worktree_plan(
            repo, [fake_task("a0", scenario="write",
                             write_file="NOTES.md", write_text="x")],
            checks={"require_changes": True})
        self.assertEqual(self.build(plan).execute()["ok"], 1)

    def test_executor_crash_gets_a_verdict_too(self) -> None:
        """汇总只认 verdict，所以任何进不去正常路径的分支也必须留下 verdict。"""
        plan = {"tasks": [fake_task("boom")]}
        orch = self.build(plan)
        with mock.patch("ma2.orchestrator.run_agent",
                        side_effect=RuntimeError("模拟崩溃")):
            summary = orch.execute()
        self.assertEqual(summary["ok"], 0)
        self.assertFalse(summary["agents"][0]["ok"])


class TestAggregation(OrchestratorCase):
    """第四阶段。两条命题：机械层无条件产出，综合层显式开启且允许失败。

    汇总 Agent 用 echo 夹具 —— 它把 prompt 原样吐回来，于是"简报有没有
    真的送到汇总 Agent 手里"变成一句可断言的话。只断言"汇总跑过了"证明不了
    这件事。
    """

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()
        self.tasks = [
            fake_task(f"a{i}", scenario="write",
                      write_file="NOTES.md", write_text=f"分支内容-{i}",
                      answer=f"答案-{i}", prompt=f"去做第 {i} 件事")
            for i in range(2)
        ]

    def aggregator(self, **cfg):
        """一个指向假 Agent 的 aggregator 配置。"""
        task = fake_task("aggregator", **cfg)
        return {"launcher": task["launcher"], "prompt": "把它们综合一下",
                "timeout_sec": task["timeout_sec"], **{
                    k: v for k, v in cfg.items()
                    if k in ("retries", "retry_delay_sec")}}

    def final_text(self) -> str:
        return self.paths.final.read_text(encoding="utf-8")

    # ------------------------------------------------------------- 机械层
    def test_final_is_written_without_any_aggregator(self) -> None:
        """没配汇总 Agent 也必须有 final.md —— 机械层不花钱、不可选。"""
        orch = self.build(self.worktree_plan(self.repo, self.tasks))
        summary = orch.execute()

        self.assertTrue(self.paths.final.exists())
        out = self.final_text()
        self.assertIn("答案-0", out)
        self.assertIn("答案-1", out)
        self.assertIn("ma2/r1/a0", out)
        self.assertIn("未启用综合层", out)
        self.assertEqual(summary["final"], str(self.paths.final))

    def test_aggregation_reads_branches_not_workspaces(self) -> None:
        """结论 7 在编排层的锁：工作区被回收之后，汇总照样拿得到产出。

        先断言工作目录真的没了 —— 否则这个测试可能是靠残留的目录通过的。
        """
        orch = self.build(self.worktree_plan(self.repo, self.tasks), cleanup=True)
        orch.execute()

        for i in range(2):
            self.assertFalse(orch.workspaces[f"a{i}"].path.exists())
        out = self.final_text()
        self.assertIn("## 产出在哪", out)
        self.assertIn("NOTES.md", out)
        brief = self.paths.brief.read_text(encoding="utf-8")
        self.assertIn("分支内容-0", brief, "分支上的文件内容必须进简报")
        self.assertIn("分支内容-1", brief)

    def test_failed_agents_still_appear_in_final(self) -> None:
        tasks = [self.tasks[0], fake_task("a1", scenario="denial")]
        orch = self.build(self.worktree_plan(self.repo, tasks))
        orch.execute()
        out = self.final_text()
        self.assertIn("FAIL(permission_denied)", out)
        self.assertIn("1/2 个 Agent 成功", out)

    # ------------------------------------------------------------- 综合层
    def test_synthesis_is_off_unless_asked_for(self) -> None:
        """综合层要花钱，因此不能因为"看起来有用"就自己跑起来。"""
        orch = self.build(self.worktree_plan(self.repo, self.tasks))
        orch.execute()
        self.assertNotIn("aggregator", orch.results)
        self.assertIn("未启用综合层", self.final_text())

    def test_brief_actually_reaches_the_aggregator(self) -> None:
        """echo 夹具把收到的 prompt 当回答吐回来，于是简报的去向可被断言。"""
        plan = self.worktree_plan(self.repo, self.tasks,
                                  aggregator=self.aggregator(scenario="echo"))
        orch = self.build(plan)
        summary = orch.execute()

        self.assertTrue(summary["aggregator"]["ok"])
        out = self.final_text()
        # 汇总 Agent 看到的必须包括：指令、各 Agent 的 prompt、回答、分支产出
        self.assertIn("把它们综合一下", out)
        self.assertIn("去做第 0 件事", out)
        self.assertIn("答案-1", out)
        self.assertIn("分支内容-0", out)
        self.assertNotIn("未启用综合层", out)

    def test_plan_entry_can_be_overridden_off(self) -> None:
        """plan 里配了也要能一键关掉 —— 排障时不该被迫为每次重跑付钱。"""
        plan = self.worktree_plan(self.repo, self.tasks,
                                  aggregator=self.aggregator(scenario="echo"))
        orch = self.build(plan, aggregate_with_agent=False)
        orch.execute()
        self.assertNotIn("aggregator", orch.results)
        self.assertIn("未启用综合层", self.final_text())

    # --------------------------------------------------- 综合失败不毁交付物
    def test_synthesis_failure_does_not_destroy_final(self) -> None:
        """核心命题：总结器挂了，这次 run 的成果不能跟着一起没。"""
        plan = self.worktree_plan(
            self.repo, self.tasks,
            aggregator=self.aggregator(scenario="crash", retries=0))
        orch = self.build(plan)
        summary = orch.execute()

        self.assertFalse(summary["aggregator"]["ok"])
        out = self.final_text()
        self.assertIn("综合层失败", out)
        self.assertIn("答案-0", out, "各 Agent 的回答必须还在")
        self.assertIn("ma2/r1/a1", out, "产出位置必须还在")

    def test_synthesis_timeout_does_not_destroy_final(self) -> None:
        plan = self.worktree_plan(
            self.repo, self.tasks,
            aggregator=self.aggregator(scenario="hang", timeout_sec=1.0,
                                       retries=0))
        orch = self.build(plan)
        summary = orch.execute()
        self.assertEqual(summary["aggregator"]["status"], P.TIMEOUT)
        self.assertIn("综合层失败", self.final_text())
        self.assertIn("答案-0", self.final_text())

    def test_aggregator_crash_is_contained(self) -> None:
        """执行器本身炸了（不是 Agent 失败）也不能拖垮收尾。"""
        plan = self.worktree_plan(self.repo, self.tasks,
                                  aggregator=self.aggregator(scenario="ok"))
        orch = self.build(plan)
        with mock.patch.object(Orchestrator, "run_with_retry",
                               side_effect=RuntimeError("炸了")) as patched:
            summary = orch.aggregate({"finished_at": P.utcnow(), "completed": 0})
        self.assertTrue(patched.called)
        self.assertFalse(summary["aggregator"]["ok"])
        self.assertTrue(self.paths.final.exists())

    # ------------------------------------------------------------ 记账口径
    def test_aggregator_is_not_counted_as_one_of_the_agents(self) -> None:
        """汇总器不是参与运算的 Agent。把它算进 ok/total 会让"2/2 成功"
        变成"2/3"，一次成功的运行看起来像失败了。"""
        plan = self.worktree_plan(self.repo, self.tasks,
                                  aggregator=self.aggregator(scenario="echo"))
        orch = self.build(plan)
        summary = orch.execute()

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["ok"], 2)
        self.assertEqual([a["agent_id"] for a in summary["agents"]], ["a0", "a1"])
        self.assertEqual(summary["status"], P.COMPLETED)

    def test_aggregator_cost_is_added_to_the_run_total(self) -> None:
        """汇总器不进 ok/total，但钱是它花的 —— 总额少算它就是账面比现实好看。"""
        plan = self.worktree_plan(self.repo, self.tasks,
                                  aggregator=self.aggregator(scenario="echo"))
        orch = self.build(plan)
        with mock.patch.object(Orchestrator, "run_aggregator",
                               return_value={"ok": True, "answer": "综合",
                                             "reasons": [], "status": P.COMPLETED,
                                             "attempt": 1, "cost_usd": 0.25}):
            summary = orch.execute()
        self.assertEqual(summary["aggregator_cost_usd"], 0.25)
        self.assertAlmostEqual(summary["total_cost_usd"],
                               summary["agents_cost_usd"] + 0.25)
        self.assertEqual(summary["total"], 2, "总额含汇总器，但计数不含")

    def test_unknown_aggregator_cost_makes_the_total_a_lower_bound(self) -> None:
        plan = self.worktree_plan(
            self.repo, self.tasks,
            aggregator=self.aggregator(scenario="hang", timeout_sec=1.0,
                                       retries=0))
        orch = self.build(plan)
        summary = orch.execute()
        self.assertFalse(summary["aggregator"]["cost_is_complete"],
                         "超时的尝试收不到 result 事件，费用无从得知")
        self.assertFalse(summary["cost_is_complete"])
        self.assertEqual(summary["total_cost_usd"], summary["agents_cost_usd"],
                         "未知费用按 0 计入，因此总额是下界")

    def test_aggregator_leaves_its_own_audit_trail(self) -> None:
        """汇总说错话时要能查它到底看到了什么、又吐了什么。"""
        plan = self.worktree_plan(self.repo, self.tasks,
                                  aggregator=self.aggregator(scenario="echo"))
        orch = self.build(plan)
        orch.execute()
        self.assertTrue(self.paths.brief.exists())
        self.assertTrue(self.paths.events("aggregator", 1).exists())
        self.assertTrue(self.paths.result("aggregator").exists())

    def test_plain_workspaces_aggregate_too(self) -> None:
        """没有 git 仓库也要能汇总，只是没有分支产出可写。"""
        orch = self.build({"run_name": "t", "tasks": [fake_task("a0")]})
        orch.execute()
        out = self.final_text()
        self.assertIn("a0", out)
        self.assertNotIn("## 产出在哪", out)


class TestPlainWorkspaces(OrchestratorCase):
    def test_plain_mode_needs_no_repo(self) -> None:
        plan = {"tasks": [fake_task("a0", scenario="write",
                                    write_file="OUT.md", write_text="x")]}
        orch = self.build(plan)
        summary = orch.execute()
        self.assertEqual(summary["status"], P.COMPLETED)
        self.assertEqual(
            (self.paths.workspace("a0") / "OUT.md").read_text(encoding="utf-8"), "x")
        self.assertIsNone(summary["agents"][0]["branch"])


if __name__ == "__main__":
    unittest.main()
