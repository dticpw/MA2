"""文件协议层。原子写是整个系统的地基：观察面随时可能在写入途中来读。"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from ma2 import protocol as P

from .support import TempDirCase


class TestWriteAtomic(TempDirCase):
    def test_roundtrip_utf8(self) -> None:
        path = self.tmp / "a.json"
        P.write_atomic(path, {"中文": "值", "n": 1})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         {"中文": "值", "n": 1})

    def test_creates_parent_dirs(self) -> None:
        path = self.tmp / "deep" / "er" / "a.json"
        P.write_atomic(path, {"ok": True})
        self.assertTrue(path.exists())

    def test_overwrite_leaves_no_temp_files(self) -> None:
        path = self.tmp / "a.json"
        for i in range(5):
            P.write_atomic(path, {"i": i})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"i": 4})
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [], "临时文件必须被 os.replace 消费掉")

    def test_failed_write_keeps_old_content_and_cleans_up(self) -> None:
        """序列化中途失败时：旧文件必须完好，临时文件必须被清掉。

        这是原子写的核心承诺 —— 读者要么看到旧的完整版本，要么看到新的
        完整版本，绝不会看到半截文件。
        """
        path = self.tmp / "a.json"
        P.write_atomic(path, {"good": True})

        class Boom:
            pass

        with self.assertRaises(TypeError):
            P.write_atomic(path, {"bad": Boom()})

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"good": True})
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [], "写失败也不许留下临时文件")

    def test_temp_file_is_created_in_same_directory(self) -> None:
        """跨卷的 os.replace 不是原子操作，临时文件必须与目标同目录。"""
        path = self.tmp / "sub" / "a.json"
        seen: list[str] = []
        real = P.tempfile.mkstemp

        def spy(*args, **kwargs):
            seen.append(kwargs.get("dir"))
            return real(*args, **kwargs)

        with mock.patch.object(P.tempfile, "mkstemp", spy):
            P.write_atomic(path, {"ok": True})
        self.assertEqual(seen, [str(path.parent)])


class TestReadJson(TempDirCase):
    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(P.read_json(self.tmp / "nope.json"))

    def test_corrupt_file_returns_none_instead_of_raising(self) -> None:
        path = self.tmp / "bad.json"
        path.write_text("{ 半截", encoding="utf-8")
        self.assertIsNone(P.read_json(path))


class TestRunPaths(TempDirCase):
    def test_layout(self) -> None:
        paths = P.RunPaths(self.tmp / "runs", "r1")
        self.assertEqual(paths.dir, self.tmp / "runs" / "r1")
        self.assertEqual(paths.events("a"), paths.agents / "a.jsonl")
        self.assertEqual(paths.status("a"), paths.agents / "a.status.json")
        self.assertEqual(paths.result("a"), paths.agents / "a.result.json")
        self.assertEqual(paths.workspace("a"), paths.workspaces / "a")
        self.assertEqual(paths.run_status, paths.dir / "status.json")

    def test_ensure_is_idempotent(self) -> None:
        paths = P.RunPaths(self.tmp / "runs", "r1")
        paths.ensure()
        paths.ensure()
        self.assertTrue(paths.agents.is_dir())
        self.assertTrue(paths.workspaces.is_dir())


class TestStatusEnum(unittest.TestCase):
    def test_terminal_set(self) -> None:
        self.assertEqual(
            P.TERMINAL,
            frozenset({P.COMPLETED, P.FAILED, P.TIMEOUT, P.CANCELLED}),
        )
        for live in (P.PENDING, P.STARTING, P.RUNNING):
            self.assertNotIn(live, P.TERMINAL)

    def test_utcnow_format(self) -> None:
        now = P.utcnow()
        self.assertTrue(now.endswith("Z"), now)
        self.assertRegex(now, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
