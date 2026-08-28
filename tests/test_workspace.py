"""工作区。worktree 的准备、提交、收集与回收，全部用真 git 跑。"""

from __future__ import annotations

import unittest

from ma2 import workspace as W

from .support import TempDirCase, git


class TestPureHelpers(unittest.TestCase):
    def test_sanitize_branch(self) -> None:
        self.assertEqual(W.sanitize_branch("ma2/run 1/agent#2"), "ma2/run-1/agent-2")
        self.assertEqual(W.sanitize_branch("///"), "agent")
        self.assertEqual(W.sanitize_branch("中文"), "agent")

    def test_resolve_spec_string_shorthand(self) -> None:
        spec = W.resolve_spec({"workspace": "worktree"}, {})
        self.assertEqual(spec["kind"], W.WORKTREE)

    def test_resolve_spec_falls_back_to_run_defaults(self) -> None:
        spec = W.resolve_spec(
            {}, {"kind": W.WORKTREE, "repo": "E:/r", "base_ref": "main"})
        self.assertEqual(spec["repo"], "E:/r")
        self.assertEqual(spec["base_ref"], "main")

    def test_task_level_overrides_run_defaults(self) -> None:
        spec = W.resolve_spec(
            {"workspace": {"kind": W.PLAIN, "repo": "E:/task"}},
            {"kind": W.WORKTREE, "repo": "E:/run"})
        self.assertEqual(spec["kind"], W.PLAIN)
        self.assertEqual(spec["repo"], "E:/task")

    def test_default_kind_is_plain(self) -> None:
        self.assertEqual(W.resolve_spec({}, {})["kind"], W.PLAIN)


class TestPrepare(TempDirCase):
    def test_plain_makes_a_directory(self) -> None:
        dest = self.tmp / "ws" / "a1"
        ws = W.prepare("a1", {"kind": W.PLAIN}, dest, "r1")
        self.assertEqual(ws.kind, W.PLAIN)
        self.assertTrue(dest.is_dir())

    def test_worktree_creates_branch_and_checkout(self) -> None:
        repo = self.make_repo()
        dest = self.tmp / "ws" / "a1"
        ws = W.prepare("a1", {"kind": W.WORKTREE, "repo": str(repo),
                              "base_ref": "main"}, dest, "r1")
        self.assertEqual(ws.branch, "ma2/r1/a1")
        self.assertTrue((dest / "README.md").exists())
        self.assertIn("ma2/r1/a1", git(repo, "branch", "--list", "ma2/*"))

    def test_worktree_without_repo_raises(self) -> None:
        with self.assertRaises(W.WorkspaceError):
            W.prepare("a1", {"kind": W.WORKTREE}, self.tmp / "ws", "r1")

    def test_non_repo_path_raises(self) -> None:
        plain = self.tmp / "notrepo"
        plain.mkdir()
        with self.assertRaises(W.WorkspaceError):
            W.prepare("a1", {"kind": W.WORKTREE, "repo": str(plain)},
                      self.tmp / "ws", "r1")

    def test_existing_destination_raises(self) -> None:
        repo = self.make_repo()
        dest = self.tmp / "ws" / "a1"
        dest.mkdir(parents=True)
        with self.assertRaises(W.WorkspaceError):
            W.prepare("a1", {"kind": W.WORKTREE, "repo": str(repo)}, dest, "r1")

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(W.WorkspaceError):
            W.prepare("a1", {"kind": "docker"}, self.tmp / "ws", "r1")


class WorktreeCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()
        self.ws = W.prepare(
            "a1", {"kind": W.WORKTREE, "repo": str(self.repo), "base_ref": "main"},
            self.tmp / "ws" / "a1", "r1")


class TestCollectChanges(WorktreeCase):
    def test_clean_worktree(self) -> None:
        changes = W.collect_changes(self.ws)
        self.assertEqual(changes["dirty_files"], 0)
        self.assertFalse(changes["committed"])
        self.assertEqual(changes["branch"], "ma2/r1/a1")

    def test_dirty_worktree(self) -> None:
        (self.ws.path / "NOTES.md").write_text("x", encoding="utf-8")
        changes = W.collect_changes(self.ws)
        self.assertEqual(changes["dirty_files"], 1)
        self.assertTrue(any("NOTES.md" in ln for ln in changes["dirty"]))

    def test_plain_workspace_yields_nothing(self) -> None:
        plain = W.Workspace(agent_id="a", kind=W.PLAIN, path=self.tmp)
        self.assertEqual(W.collect_changes(plain), {})


class TestCommitChanges(WorktreeCase):
    def test_commit_moves_head(self) -> None:
        (self.ws.path / "NOTES.md").write_text("内容", encoding="utf-8")
        info = W.commit_changes(self.ws, "测试提交")
        self.assertTrue(info["committed"])
        changes = W.collect_changes(self.ws)
        self.assertTrue(changes["committed"])
        self.assertEqual(changes["dirty_files"], 0)

    def test_clean_worktree_is_a_noop(self) -> None:
        info = W.commit_changes(self.ws, "空提交")
        self.assertFalse(info["committed"])
        self.assertIn("干净", info["reason"])

    def test_does_not_depend_on_machine_git_identity(self) -> None:
        """提交者身份由编排器显式给出，不依赖机器上配了 user.name。"""
        (self.ws.path / "NOTES.md").write_text("x", encoding="utf-8")
        W.commit_changes(self.ws, "m")
        author = git(self.ws.path, "log", "-1", "--format=%an <%ae>")
        self.assertEqual(author, "ma2-orchestrator <ma2@localhost>")

    def test_untracked_files_are_included(self) -> None:
        (self.ws.path / "新文件.md").write_text("x", encoding="utf-8")
        W.commit_changes(self.ws, "m")
        listed = git(self.ws.path, "ls-tree", "--name-only", "HEAD")
        self.assertIn("新文件.md", listed)


class TestRemove(WorktreeCase):
    def test_remove_keeps_branch(self) -> None:
        self.assertEqual(W.remove(self.ws), "removed")
        self.assertFalse(self.ws.path.exists())
        self.assertIn("ma2/r1/a1", git(self.repo, "branch", "--list", "ma2/*"))

    def test_remove_can_delete_branch(self) -> None:
        W.remove(self.ws, delete_branch=True)
        self.assertEqual(git(self.repo, "branch", "--list", "ma2/*"), "")

    def test_uncommitted_work_is_destroyed_by_remove(self) -> None:
        """把已知的危险行为钉死，防止有人误以为 remove 是安全的。

        README 结论 7：Agent 不会自己 commit，remove --force 会连未提交改动
        一起删。"保留分支"在没提交的前提下是空承诺。
        """
        (self.ws.path / "NOTES.md").write_text("会丢", encoding="utf-8")
        W.remove(self.ws)
        with self.assertRaises(RuntimeError):
            git(self.repo, "show", "ma2/r1/a1:NOTES.md")

    def test_commit_then_remove_preserves_work(self) -> None:
        """结论 7 的正向回归：先提交，产出才真正活在分支上。"""
        (self.ws.path / "NOTES.md").write_text("会留下", encoding="utf-8")
        W.commit_changes(self.ws, "保住它")
        W.remove(self.ws)
        self.assertFalse(self.ws.path.exists())
        self.assertEqual(git(self.repo, "show", "ma2/r1/a1:NOTES.md"), "会留下")

    def test_is_dirty(self) -> None:
        self.assertFalse(W.is_dirty(self.ws))
        (self.ws.path / "NOTES.md").write_text("x", encoding="utf-8")
        self.assertTrue(W.is_dirty(self.ws))

    def test_remove_plain_is_a_noop(self) -> None:
        plain = W.Workspace(agent_id="a", kind=W.PLAIN, path=self.tmp)
        self.assertEqual(W.remove(plain), "skipped")


class TestReset(WorktreeCase):
    """重试前的工作区重置。"""

    def test_reset_discards_new_and_modified_files(self) -> None:
        """重试要从确定状态起步，否则第二次尝试面对的是上一次的半成品。"""
        (self.ws.path / "NEW.md").write_text("新建的", encoding="utf-8")
        (self.ws.path / "README.md").write_text("被改过", encoding="utf-8")
        info = W.reset(self.ws)
        self.assertTrue(info["reset"])
        self.assertEqual(info["discarded"], 2)
        self.assertFalse((self.ws.path / "NEW.md").exists())
        self.assertEqual((self.ws.path / "README.md").read_text(encoding="utf-8"),
                         "base\n")
        self.assertFalse(W.is_dirty(self.ws))

    def test_reset_keeps_commits_already_made(self) -> None:
        """清的是未提交的残留，不是回滚已经落到分支上的东西。"""
        (self.ws.path / "DONE.md").write_text("已提交", encoding="utf-8")
        W.commit_changes(self.ws, "第一次的成果")
        head = git(self.ws.path, "rev-parse", "HEAD")
        (self.ws.path / "JUNK.md").write_text("残留", encoding="utf-8")
        W.reset(self.ws)
        self.assertFalse((self.ws.path / "JUNK.md").exists())
        self.assertTrue((self.ws.path / "DONE.md").exists())
        self.assertEqual(git(self.ws.path, "rev-parse", "HEAD"), head)

    def test_reset_on_a_clean_worktree_is_harmless(self) -> None:
        self.assertEqual(W.reset(self.ws)["discarded"], 0)


class TestResetPlain(TempDirCase):
    def test_reset_empties_a_plain_workspace(self) -> None:
        ws = W.Workspace(agent_id="a", kind=W.PLAIN, path=self.tmp / "ws")
        ws.path.mkdir()
        (ws.path / "f.txt").write_text("x", encoding="utf-8")
        (ws.path / "sub").mkdir()
        (ws.path / "sub" / "g.txt").write_text("y", encoding="utf-8")
        info = W.reset(ws)
        self.assertEqual(info["discarded"], 2)
        self.assertEqual(list(ws.path.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
