"""单 Agent 执行器。用假 Agent 跑真子进程，不花钱不联网。"""

from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from ma2 import protocol as P
from ma2 import runner as R

from .support import TempDirCase, fake_task


class TestBuildEnv(unittest.TestCase):
    """§9 凭据边界的执行点。这几条一旦松掉，密钥就会漏进子进程。"""

    def test_only_allowlisted_vars_pass_through(self) -> None:
        with mock.patch.dict(os.environ, {"SECRET_SAUCE": "x", "PATH": "p"}, clear=True):
            env = R.build_env()
        self.assertNotIn("SECRET_SAUCE", env)
        self.assertEqual(env.get("PATH"), "p")

    def test_anthropic_vars_are_not_forwarded_by_default(self) -> None:
        fake = {"ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_BASE_URL": "u",
                "CLAUDE_CODE_X": "y", "PATH": "p"}
        with mock.patch.dict(os.environ, fake, clear=True):
            env = R.build_env()
        leaked = [k for k in env if k.startswith(("ANTHROPIC_", "CLAUDE_"))]
        self.assertEqual(leaked, [], "默认绝不转发凭据类变量")

    def test_forward_anthropic_is_opt_in_only(self) -> None:
        fake = {"ANTHROPIC_AUTH_TOKEN": "tok", "PATH": "p"}
        with mock.patch.dict(os.environ, fake, clear=True):
            env = R.build_env(forward_anthropic=True)
        self.assertEqual(env.get("ANTHROPIC_AUTH_TOKEN"), "tok")

    def test_explicit_injection_wins(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "p"}, clear=True):
            env = R.build_env({"MY_VAR": "v", "PATH": "override"})
        self.assertEqual(env["MY_VAR"], "v")
        self.assertEqual(env["PATH"], "override")


class TestBuildArgv(unittest.TestCase):
    def test_headless_flags_always_present(self) -> None:
        argv = R.build_argv(["claude"], {"id": "a", "prompt": "p"})
        self.assertEqual(argv[:5],
                         ["claude", "-p", "--output-format", "stream-json", "--verbose"])

    def test_prompt_never_goes_into_argv(self) -> None:
        """prompt 走 stdin：Windows 命令行有长度上限，引号换行也极易出错。"""
        argv = R.build_argv(["claude"], {"id": "a", "prompt": "带 \"引号\" 和\n换行"})
        self.assertNotIn("带 \"引号\" 和\n换行", argv)

    def test_optional_flags(self) -> None:
        argv = R.build_argv(["claude"], {
            "id": "a", "prompt": "p", "model": "m",
            "permission_mode": "acceptEdits",
            "allowed_tools": ["Read", "Write"],
            "disallowed_tools": ["Bash"],
            "system_suffix": "契约",
        })
        self.assertIn("--allowedTools", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Write")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Bash")
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "契约")

    def test_multi_segment_launcher_is_preserved(self) -> None:
        argv = R.build_argv(["py", "fake.py", "--scenario", "ok"],
                            {"id": "a", "prompt": "p"})
        self.assertEqual(argv[:4], ["py", "fake.py", "--scenario", "ok"])


class TestResolveLauncher(unittest.TestCase):
    def test_override_string(self) -> None:
        self.assertEqual(R.resolve_launcher("claude", "C:/x/claude.cmd"),
                         ["C:/x/claude.cmd"])

    def test_override_list(self) -> None:
        self.assertEqual(R.resolve_launcher("anything", ["py", "a.py"]),
                         ["py", "a.py"])

    def test_unknown_kind_without_override_raises(self) -> None:
        with self.assertRaises(R.AgentLaunchError):
            R.resolve_launcher("codex")


class RunAgentCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths = P.RunPaths(self.tmp / "runs", "r1")
        self.paths.ensure()
        self.ws = self.tmp / "ws"
        self.ws.mkdir()

    def run_task(self, task):
        return R.run_agent(task, self.paths, workspace=self.ws)


class TestRunAgentHappyPath(RunAgentCase):
    def test_completed_and_artifacts_written(self) -> None:
        res = self.run_task(fake_task("a1", scenario="ok", answer="你好"))
        self.assertEqual(res["status"], P.COMPLETED)
        self.assertEqual(res["answer"], "你好")
        self.assertEqual(res["exit_code"], 0)
        self.assertTrue(self.paths.events("a1").exists())
        self.assertTrue(self.paths.status("a1").exists())
        self.assertTrue(self.paths.result("a1").exists())

    def test_events_file_is_valid_jsonl(self) -> None:
        self.run_task(fake_task("a1", scenario="ok"))
        lines = self.paths.events("a1").read_text(encoding="utf-8").splitlines()
        types = [json.loads(ln)["type"] for ln in lines if ln.strip()]
        self.assertEqual(types, ["system", "assistant", "result"])

    def test_result_json_matches_returned_document(self) -> None:
        res = self.run_task(fake_task("a1", scenario="ok"))
        on_disk = P.read_json(self.paths.result("a1"))
        self.assertEqual(on_disk["status"], res["status"])
        self.assertEqual(on_disk["answer"], res["answer"])

    def test_callbacks_fire(self) -> None:
        logs: list[str] = []
        updates: list[tuple[str, str]] = []
        R.run_agent(
            fake_task("a1", scenario="ok"), self.paths, workspace=self.ws,
            log=logs.append,
            on_update=lambda aid, snap: updates.append((aid, snap["status"])),
        )
        self.assertTrue(any("launch" in ln for ln in logs))
        self.assertEqual(updates[-1], ("a1", P.COMPLETED))

    def test_agent_runs_in_the_given_workspace(self) -> None:
        self.run_task(fake_task("a1", scenario="write",
                                write_file="OUT.md", write_text="内容"))
        self.assertEqual((self.ws / "OUT.md").read_text(encoding="utf-8"), "内容")


class TestRunAgentFailurePaths(RunAgentCase):
    def test_is_error_result_is_failed(self) -> None:
        res = self.run_task(fake_task("a1", scenario="crash"))
        self.assertEqual(res["status"], P.FAILED)
        self.assertEqual(res["exit_code"], 1)

    def test_exit_without_result_event_is_failed(self) -> None:
        """进程正常退出却没吐 result —— 编排器必须自己兜底，不能停在 running。"""
        res = self.run_task(fake_task("a1", scenario="silent"))
        self.assertEqual(res["status"], P.FAILED)

    def test_no_init_at_all_is_failed(self) -> None:
        res = self.run_task(fake_task("a1", scenario="no-init"))
        self.assertEqual(res["status"], P.FAILED)

    def test_permission_denial_completes_but_is_flagged(self) -> None:
        """README 结论 3：status == completed 不足以判定成功。"""
        res = self.run_task(fake_task("a1", scenario="denial"))
        self.assertEqual(res["status"], P.COMPLETED)
        self.assertEqual(len(res["diagnostics"]["permission_denials"]), 1)

    def test_malformed_lines_are_persisted_verbatim_and_counted(self) -> None:
        """审计流的完整性优先于解析成功与否：坏行也必须原样落盘。"""
        res = self.run_task(fake_task("a1", scenario="garbage"))
        self.assertEqual(res["status"], P.COMPLETED)
        self.assertEqual(res["metrics"]["malformed_lines"], 3)
        raw = self.paths.events("a1").read_text(encoding="utf-8")
        self.assertIn("这不是 JSON", raw)
        self.assertIn("{\"truncated\":", raw)


class TestRunAgentTimeout(RunAgentCase):
    """README 结论 4 的回归测试。

    Windows 上 proc.kill() 只杀直接子进程；孙进程继续持有 stdout，读取循环
    永远阻塞 —— 超时只被"检测"到、没被"执行"。实测代价是一个 timeout_sec=8
    的任务跑了 6 分 40 秒。这两个测试用墙钟把"执行"钉死。
    """

    TIMEOUT = 2.0
    BUDGET = 25.0  # 宽松但远小于"没杀掉"时的行为

    def test_simple_hang_is_killed(self) -> None:
        t0 = time.monotonic()
        res = self.run_task(fake_task("a1", scenario="hang", timeout_sec=self.TIMEOUT))
        elapsed = time.monotonic() - t0
        self.assertEqual(res["status"], P.TIMEOUT)
        self.assertTrue(res["diagnostics"]["timeout_fired"])
        self.assertLess(elapsed, self.BUDGET, f"超时未被执行，耗时 {elapsed:.1f}s")

    def test_grandchild_holding_stdout_is_also_killed(self) -> None:
        """孙进程继承 stdout。只杀直接子进程的话这个测试会挂到超时。"""
        t0 = time.monotonic()
        res = self.run_task(fake_task("a1", scenario="hang-tree",
                                      timeout_sec=self.TIMEOUT))
        elapsed = time.monotonic() - t0
        self.assertEqual(res["status"], P.TIMEOUT)
        self.assertLess(elapsed, self.BUDGET,
                        f"进程树没被杀干净，耗时 {elapsed:.1f}s")

    def test_watchdog_does_not_overwrite_a_real_completion(self) -> None:
        """曾经的 bug：超时标记无条件覆盖，把真完成改写成 timeout。

        现在只在非终态时才 mark，watchdog 触发与否只进 diagnostics。
        """
        res = self.run_task(fake_task("a1", scenario="ok", timeout_sec=30))
        self.assertEqual(res["status"], P.COMPLETED)
        self.assertFalse(res["diagnostics"]["timeout_fired"])
        self.assertEqual(res["diagnostics"]["timeout_sec"], 30)


class TestResourceHygiene(RunAgentCase):
    """编排器会并行反复起 Agent，任何每次泄漏一点的东西都会累积。"""

    def test_pipes_are_closed(self) -> None:
        captured: list = []
        real_popen = R.subprocess.Popen

        def spy(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            captured.append(proc)
            return proc

        with mock.patch.object(R.subprocess, "Popen", spy):
            self.run_task(fake_task("a1", scenario="ok"))

        self.assertEqual(len(captured), 1)
        proc = captured[0]
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(proc, name)
            self.assertTrue(stream is None or stream.closed,
                            f"{name} 没关，会持续泄漏文件描述符")

    def test_no_resource_warnings(self) -> None:
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.run_task(fake_task("a1", scenario="ok"))
        leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(leaks, [], f"存在未释放资源: {[str(w.message) for w in leaks]}")

    def test_pipes_closed_even_on_timeout(self) -> None:
        captured: list = []
        real_popen = R.subprocess.Popen

        def spy(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            captured.append(proc)
            return proc

        with mock.patch.object(R.subprocess, "Popen", spy):
            res = self.run_task(fake_task("a1", scenario="hang", timeout_sec=2))

        self.assertEqual(res["status"], P.TIMEOUT)
        self.assertTrue(captured[0].stdout.closed)


if __name__ == "__main__":
    unittest.main()
