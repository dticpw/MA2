"""单 Agent 执行器。

第一步只做一件事：把一个 headless Agent 当成普通子进程跑起来，把它的事件流
原样落盘，归约出终态，产出 result.json。

刻意不做的事（留给后续步骤）：
    - 并行与 worktree 隔离  → 第二步
    - 超时重试策略          → 第三步（这里只有单次超时，不重试）
    - 多 Agent 汇总         → 第四步

设计要点：Agent 是 headless 子进程，不是交互式终端会话。因此这里没有 PTY、
没有多路复用、没有按键注入，进程环境由编排器显式构造（handoff.md §9）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import protocol as P
from .events import AgentState

# 传给子进程的环境变量白名单。凡是不在这里、也没被显式注入的，Agent 一律看不到。
# 这是 §9 凭据边界的执行点：默认不转发任何 ANTHROPIC_* / 密钥类变量。
ENV_ALLOWLIST = (
    "SystemRoot", "windir", "COMSPEC", "PATH", "PATHEXT",
    "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH",
    "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE",
)

STATUS_WRITE_MIN_INTERVAL = 0.4  # 秒。状态变化和终态无视节流，一定写。

KILL_GRACE_SEC = 10.0  # 杀进程树后等待管道关闭的宽限


class AgentLaunchError(RuntimeError):
    pass


def terminate_tree(proc: subprocess.Popen) -> None:
    """杀掉整棵进程树。

    Windows 上这是必须的：npm 装的 claude 是 claude.cmd，真正干活的 node 是
    cmd.exe 派生的子进程。proc.kill() 只杀 cmd.exe，node 会继续跑并持有 stdout
    管道，导致读取循环一直阻塞 —— 超时于是只能被"检测"到，无法被"执行"。
    taskkill /T 才会连子孙进程一起终止。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, check=False,
        )
    else:
        proc.kill()


def build_env(inject: dict[str, str] | None = None, forward_anthropic: bool = False) -> dict[str, str]:
    """按白名单构造子进程环境。

    forward_anthropic 仅用于排障。正常路径下 claude 自己从 settings.json 读取
    认证配置，编排器完全不碰凭据 —— 这样凭据既不出现在命令行，也不出现在
    子进程环境里。
    """
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    if forward_anthropic:
        for k, v in os.environ.items():
            if k.startswith(("ANTHROPIC_", "CLAUDE_")):
                env[k] = v
    env.update(inject or {})
    return env


def resolve_launcher(agent_kind: str) -> str:
    """把 Agent 类型解析成可执行文件绝对路径。

    Windows 上 npm 装的 claude 是 claude.cmd；Python 的 subprocess 能直接拉起
    .cmd，但必须给绝对路径，不能依赖 PATH 解析。
    """
    if agent_kind == "claude":
        for cand in ("claude.cmd", "claude.exe", "claude"):
            found = shutil.which(cand)
            if found:
                return found
        raise AgentLaunchError("PATH 上找不到 claude，先确认 npm 全局 bin 在 PATH 里")
    raise AgentLaunchError(f"第一步只支持 agent=claude，收到 {agent_kind!r}")


def build_argv(launcher: str, task: dict[str, Any]) -> list[str]:
    """构造 headless 调用。

    prompt 走 stdin 而不是 argv：Windows 命令行有长度上限，而且 prompt 里
    的引号和换行走 argv 极易出错。
    """
    argv = [
        launcher,
        "-p",
        "--output-format", "stream-json",
        "--verbose",  # stream-json 在 print 模式下需要它才会吐完整事件
    ]
    if task.get("model"):
        argv += ["--model", str(task["model"])]
    if task.get("permission_mode"):
        argv += ["--permission-mode", str(task["permission_mode"])]
    if task.get("allowed_tools"):
        argv += ["--allowedTools", ",".join(task["allowed_tools"])]
    if task.get("disallowed_tools"):
        argv += ["--disallowedTools", ",".join(task["disallowed_tools"])]
    if task.get("system_suffix"):
        argv += ["--append-system-prompt", str(task["system_suffix"])]
    return argv


def run_agent(task: dict[str, Any], paths: P.RunPaths, *, workspace: Path,
              forward_anthropic: bool = False,
              log: Callable[[str], None] | None = None,
              on_update: Callable[[str, dict[str, Any]], None] | None = None,
              ) -> dict[str, Any]:
    """跑一个 Agent 到终态，返回 result 文档。

    workspace 由调用方预先准备好（见 ma2.workspace）。worktree 的创建必须串行，
    因此不能放在这里 —— 这个函数会被并行调用。
    """
    agent_id = task["id"]
    say = log or (lambda _msg: None)

    launcher = resolve_launcher(task.get("agent", "claude"))
    argv = build_argv(launcher, task)
    env = build_env(task.get("env_inject"), forward_anthropic=forward_anthropic)
    timeout_sec = float(task.get("timeout_sec", 600))

    state = AgentState(agent_id, paths.run_id)
    events_path = paths.events(agent_id)
    status_path = paths.status(agent_id)

    def publish() -> None:
        snap = state.snapshot()
        P.write_atomic(status_path, snap)
        if on_update:
            on_update(agent_id, snap)

    publish()
    say(f"launch  {Path(launcher).name}  ws={workspace.name}  timeout={timeout_sec:g}s")

    proc = subprocess.Popen(
        argv,
        cwd=str(workspace),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        terminate_tree(proc)

    watchdog = threading.Timer(timeout_sec, _kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()

    # stderr 必须并发抽干，否则管道写满会把子进程卡死
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    # prompt 走 stdin，写完立刻关闭，让 Agent 知道输入结束
    try:
        assert proc.stdin is not None
        proc.stdin.write(task["prompt"])
        proc.stdin.close()
    except OSError as exc:
        state.mark(P.FAILED, f"stdin 写入失败: {exc}")

    last_status_write = 0.0
    prev_status = state.status

    with open(events_path, "w", encoding="utf-8", newline="\n") as sink:
        assert proc.stdout is not None
        for line in proc.stdout:
            # 无条件先原样落盘再解析。审计流的完整性优先于解析成功与否（§13）。
            sink.write(line if line.endswith("\n") else line + "\n")
            sink.flush()

            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                state.malformed_lines += 1
                continue
            if not isinstance(event, dict):
                state.malformed_lines += 1
                continue

            state.update(event)
            if state.last_activity:
                say(f"{state.status:<9} {state.last_activity}")

            now = time.monotonic()
            if state.status != prev_status or (now - last_status_write) >= STATUS_WRITE_MIN_INTERVAL:
                publish()
                last_status_write = now
                prev_status = state.status

    try:
        exit_code = proc.wait(timeout=KILL_GRACE_SEC)
    except subprocess.TimeoutExpired:
        # 管道已关闭但进程还没收尸，再补一刀
        terminate_tree(proc)
        exit_code = proc.wait(timeout=KILL_GRACE_SEC)
    watchdog.cancel()
    stderr_thread.join(timeout=5)

    stderr_text = "".join(stderr_chunks)
    if stderr_text.strip():
        paths.stderr(agent_id).write_text(stderr_text, encoding="utf-8")

    # 终态兜底。
    # 注意顺序：已经收到 result 事件就以 result 为准，不让超时把一次真实完成
    # 改写成 timeout —— 只在 diagnostics 里记下 watchdog 曾经触发。
    if state.status not in P.TERMINAL:
        if timed_out.is_set():
            state.mark(P.TIMEOUT, f"超过 {timeout_sec:g}s 被终止")
        else:
            state.mark(P.FAILED, f"进程退出 code={exit_code} 但未收到 result 事件")

    P.write_atomic(status_path, state.snapshot())
    publish()
    result = state.result_document(exit_code, str(events_path))
    result["diagnostics"]["timeout_fired"] = timed_out.is_set()
    result["diagnostics"]["timeout_sec"] = timeout_sec
    result["workspace"] = {"path": str(workspace)}
    P.write_atomic(paths.result(agent_id), result)
    say(f"{state.status}  ({(state.duration_ms or 0) / 1000:.1f}s)")
    return result
