"""claude 兼容的测试替身。

按 --scenario 吐一段脚本化的 stream-json，让编排层能在不花钱、不联网、
毫秒级完成的条件下被完整测试。事件 schema 照抄 ma2/events.py 顶部记录的
2026-08-28 实测结果。

它同时是几个已修 bug 的回归夹具：
    hang-tree   复现 Windows 进程树问题 —— 派生一个持有 stdout 的孙进程。
                如果 terminate_tree 退回 proc.kill()，读取循环会永远阻塞。
    garbage     非 JSON 行必须原样进审计流，且被计入 malformed_lines。
    silent      进程正常退出却没有 result 事件，编排器必须自己兜底成 failed。
    flaky       前 N 次失败、之后成功。重试策略唯一能被证伪的夹具 ——
                只有同一个 task 两次跑出不同结果，才说明重试真的发生了。

真 claude 的参数一律接受并忽略：这个替身要能被 build_argv 原样调用。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def force_utf8_stdout() -> None:
    """编排器按 UTF-8 读子进程 stdout，替身必须照此输出。

    Windows 上 Python 的 stdout 默认走系统 ANSI 代码页（本机是 GBK），
    不改的话中文会变成乱码 —— 而真 claude 是 node，本来就吐 UTF-8。
    替身要在这一点上忠实，否则测出来的是替身的毛病不是被测代码的。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass


def init_event(session: str, cwd: str) -> dict:
    return {
        "type": "system", "subtype": "init",
        "session_id": session, "cwd": cwd,
        "model": "fake-model", "permissionMode": "acceptEdits",
        "tools": ["Read", "Write"],
    }


def result_event(session: str, *, answer: str, is_error: bool = False,
                 denials: list | None = None, turns: int = 1) -> dict:
    return {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "session_id": session,
        "is_error": is_error,
        "stop_reason": "end_turn",
        "terminal_reason": "done",
        "num_turns": turns,
        "duration_ms": 123,
        "duration_api_ms": 100,
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "permission_denials": denials or [],
        "result": answer,
    }


def bump_counter(path: str | None) -> int:
    """把调用次数记在工作区外的文件里，返回这是第几次（从 1 开始）。"""
    if not path:
        return 1
    try:
        n = int(open(path, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        n = 0
    n += 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(n))
    return n


def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--scenario", default="ok")
    ap.add_argument("--write-file", default=None)
    ap.add_argument("--write-text", default=None)
    ap.add_argument("--answer", default="ok")
    ap.add_argument("--hang-sec", type=float, default=600.0)
    # flaky：靠计数文件跨进程记住"这是第几次尝试"。计数文件必须放在工作区
    # 外面 —— 重试前工作区会被重置，放里面就永远数不过 1。
    ap.add_argument("--counter-file", default=None)
    ap.add_argument("--fail-times", type=int, default=1)
    ap.add_argument("--fail-as", default="crash")
    # 真 claude 的参数，接受并丢弃
    ap.add_argument("-p", action="store_true")
    ap.add_argument("--output-format", default=None)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--permission-mode", default=None)
    ap.add_argument("--allowedTools", default=None)
    ap.add_argument("--disallowedTools", default=None)
    ap.add_argument("--append-system-prompt", default=None)
    args, _unknown = ap.parse_known_args()

    # 编排器把 prompt 写进 stdin 后立刻关闭；不读走会让对端写入报错
    try:
        prompt = sys.stdin.read()
    except OSError:
        prompt = ""

    session = str(uuid.uuid4())
    cwd = os.getcwd()
    scenario = args.scenario

    if scenario == "flaky":
        # 前 fail-times 次按 fail-as 失败，之后成功。重试策略的核心夹具：
        # 只有"同一个 task 第二次跑出不同结果"才能证明重试真的发生了。
        n = bump_counter(args.counter_file)
        if args.write_file:
            # 每次尝试写不同的文件名，工作区有没有被重置就能被直接看出来
            args.write_file = f"{n}-{args.write_file}"
        scenario = args.fail_as if n <= args.fail_times else "write"

    if scenario == "no-init":
        # 连 init 都没有就直接退出
        return 0

    emit(init_event(session, cwd))

    if scenario == "hang":
        time.sleep(args.hang_sec)
        return 0

    if scenario == "hang-tree":
        # 关键：孙进程继承 stdout。只杀直接子进程的话管道不会关闭，
        # 编排器的读取循环会永远卡住 —— 这正是 taskkill /T 要解决的问题。
        subprocess.Popen(
            [sys.executable, "-c",
             f"import time; time.sleep({args.hang_sec})"],
            stdout=sys.stdout, stderr=subprocess.DEVNULL,
        )
        time.sleep(args.hang_sec)
        return 0

    if scenario == "garbage":
        sys.stdout.write("这不是 JSON\n")
        sys.stdout.write("{\"truncated\": \n")
        sys.stdout.write("[1, 2, 3]\n")  # 合法 JSON 但不是对象
        sys.stdout.flush()
        emit(result_event(session, answer=args.answer))
        return 0

    if scenario == "silent":
        # 正常退出，但没有 result 事件
        return 0

    if scenario == "crash":
        emit(result_event(session, answer="炸了", is_error=True))
        return 1

    if scenario == "denial":
        emit(result_event(
            session, answer="我没有 Bash 权限，什么也没做成。",
            denials=[{"tool_name": "Bash", "tool_use_id": "tu_1"}],
        ))
        return 0

    if scenario == "write-crash":
        # 留下半成品再失败 —— 重试前该不该清掉它，正是 reset 要回答的问题
        with open(args.write_file or "NOTES.md", "w", encoding="utf-8") as fh:
            fh.write(args.write_text if args.write_text is not None else "半成品")
        emit(result_event(session, answer="写了一半就炸了", is_error=True))
        return 1

    if scenario == "empty":
        # 正常收尾但一个字都没给。status 是 completed，任务却没有产出。
        emit(result_event(session, answer=""))
        return 0

    if scenario == "write":
        # 真写文件：隔离测试要靠它证明各 Agent 互不覆盖
        path = args.write_file or "NOTES.md"
        text = args.write_text if args.write_text is not None else prompt
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "id": "tu_1",
             "input": {"file_path": path}},
        ]}})
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
        ]}})
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": args.answer},
        ]}})
        emit(result_event(session, answer=args.answer, turns=2))
        return 0

    # 默认 ok
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": args.answer},
    ]}})
    emit(result_event(session, answer=args.answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
