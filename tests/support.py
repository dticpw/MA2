"""测试公共设施：临时 git 仓库、指向假 Agent 的 task 构造。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
FAKE_AGENT = TESTS_DIR / "fake_agent.py"

# 用当前解释器跑假 Agent，不依赖 PATH 上有什么 python
LAUNCHER = [sys.executable, str(FAKE_AGENT)]


def fake_task(agent_id: str, *, scenario: str = "ok", timeout_sec: float = 30.0,
              **extra: Any) -> dict[str, Any]:
    """构造一个指向假 Agent 的 task。

    launcher 之后的参数会被 build_argv 原样透传给假 Agent。
    """
    flags: list[str] = ["--scenario", scenario]
    for key in ("write_file", "write_text", "answer", "hang_sec",
                "counter_file", "fail_times", "fail_as"):
        if key in extra:
            flags += [f"--{key.replace('_', '-')}", str(extra.pop(key))]
    task: dict[str, Any] = {
        "id": agent_id,
        "launcher": [*LAUNCHER, *flags],
        "prompt": extra.pop("prompt", "测试用 prompt"),
        "timeout_sec": timeout_sec,
    }
    task.update(extra)
    return task


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotePath=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


class TempDirCase(unittest.TestCase):
    """给每个测试一个独立临时目录，结束后清理。"""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ma2-test-")
        self.tmp = Path(self._tmp)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        # Windows 上 git 会留下只读的 object 文件，rmtree 需要放宽权限
        def _onerror(func, path, _exc):
            import os
            import stat
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except OSError:
                pass

        shutil.rmtree(self._tmp, onerror=_onerror)

    def make_repo(self, name: str = "repo") -> Path:
        """建一个带初始提交、分支名固定为 main 的 git 仓库。

        身份用 -c 显式给出：测试不该依赖跑它的机器配了 user.name。
        """
        repo = self.tmp / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo,
            "-c", "user.name=test", "-c", "user.email=test@localhost",
            "commit", "-q", "-m", "init")
        return repo
