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
