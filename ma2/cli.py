"""命令行入口。

    python -m ma2 run plans/hello.json
    python -m ma2 show runs/<run_id>
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from . import protocol as P
from .runner import run_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS = REPO_ROOT / "runs"


def new_run_id(name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{name}-{uuid.uuid4().hex[:6]}"


def cmd_run(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    tasks = plan.get("tasks") or []
    if not tasks:
        print(f"plan 里没有 tasks: {plan_path}", file=sys.stderr)
        return 2

    run_id = args.run_id or new_run_id(plan.get("run_name", "run"))
    paths = P.RunPaths(Path(args.runs_root), run_id)
    paths.ensure()

    P.write_atomic(paths.run_json, {
        "run_id": run_id,
        "run_name": plan.get("run_name"),
        "plan_path": str(plan_path),
        "created_at": P.utcnow(),
        "task_ids": [t["id"] for t in tasks],
        "status": P.RUNNING,
    })
    print(f"run_id: {run_id}")
    print(f"run_dir: {paths.dir}\n")

    # 第一步刻意串行。并行是第二步，而且要连 worktree 隔离一起做。
    results = []
    for task in tasks:
        results.append(run_agent(
            task, paths,
            forward_anthropic=args.forward_anthropic,
            echo=not args.quiet,
        ))
        print()

    ok = sum(1 for r in results if r["status"] == P.COMPLETED)
    P.write_atomic(paths.run_json, {
        **(P.read_json(paths.run_json) or {}),
        "finished_at": P.utcnow(),
        "status": P.COMPLETED if ok == len(results) else P.FAILED,
        "completed": ok,
        "total": len(results),
    })

    for r in results:
        m = r["metrics"]
        cost = m.get("total_cost_usd")
        secs = (m.get("duration_ms") or 0) / 1000
        cost_s = f"${cost:.4f}" if isinstance(cost, (int, float)) else "-"
        print(f"{r['agent_id']:<16} {r['status']:<10} "
              f"turns={m.get('num_turns')!s:<5} {secs:>6.1f}s  {cost_s}")

    print(f"\n{ok}/{len(results)} completed  ->  {paths.dir}")
    return 0 if ok == len(results) else 1


def cmd_show(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    for path in sorted((run_dir / "agents").glob("*.result.json")):
        doc = P.read_json(path) or {}
        print("=" * 72)
        print(f"{doc.get('agent_id')}  status={doc.get('status')}  "
              f"session={doc.get('session_id')}")
        print("=" * 72)
        print(doc.get("answer") or "(no answer)")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    # 被重定向到文件或管道时 Python 默认块缓冲，进度会攒到进程结束才出现。
    # 观察面（tail 日志）要求实时性，这里强制行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(prog="ma2", description="headless 多 Agent 编排")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行一个 plan")
    p_run.add_argument("plan")
    p_run.add_argument("--runs-root", default=str(DEFAULT_RUNS))
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--quiet", action="store_true")
    p_run.add_argument("--forward-anthropic", action="store_true",
                       help="排障用：把 ANTHROPIC_*/CLAUDE_* 转发进子进程环境")
    p_run.set_defaults(func=cmd_run)

    p_show = sub.add_parser("show", help="打印某次 run 的各 Agent 回答")
    p_show.add_argument("run_dir")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
