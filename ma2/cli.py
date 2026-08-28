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
from . import workspace as W
from .orchestrator import Orchestrator

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

    ids = [t["id"] for t in tasks]
    if len(set(ids)) != len(ids):
        print(f"plan 里有重复的 task id: {ids}", file=sys.stderr)
        return 2

    run_id = args.run_id or new_run_id(plan.get("run_name", "run"))
    if args.retries is not None:
        # 显式给的开关压过 plan 的 run 级默认；task 级仍然最高优先
        plan["retries"] = args.retries
    paths = P.RunPaths(Path(args.runs_root), run_id)
    paths.ensure()

    P.write_atomic(paths.run_json, {
        "run_id": run_id,
        "run_name": plan.get("run_name"),
        "plan_path": str(plan_path),
        "created_at": P.utcnow(),
        "task_ids": ids,
        "status": P.RUNNING,
    })
    print(f"run_id : {run_id}")
    print(f"run_dir: {paths.dir}")
    print(f"观察   : {paths.run_status}\n")

    orch = Orchestrator(
        plan, paths,
        max_parallel=args.max_parallel,
        forward_anthropic=args.forward_anthropic,
        quiet=args.quiet,
        cleanup=args.cleanup,
        autocommit=args.autocommit,
    )
    summary = orch.execute()

    print(f"\n{'agent':<14} {'verdict':<8} {'status':<10} {'try':>3} "
          f"{'branch':<22} {'dirty':>5} {'sec':>7}  cost")
    print("-" * 88)
    for a in summary["agents"]:
        cost = a.get("total_cost_usd")
        # 超时的尝试没有费用数据，按 0 计入 —— 打 ≥ 是提醒这是下界不是实测值
        lead = "" if a.get("cost_is_complete", True) else "≥"
        cost_s = f"{lead}${cost:.4f}" if isinstance(cost, (int, float)) else "-"
        secs = (a.get("duration_ms") or 0) / 1000
        # 分支名前缀是 run_id，上面已经打过了，这里只显示末段
        branch = (a.get("branch") or "-").rsplit("/", 1)[-1]
        why = f"  {'+'.join(a.get('reasons') or [])}" if not a.get("ok") else ""
        print(f"{a['agent_id']:<14} {'OK' if a.get('ok') else 'FAIL':<8} "
              f"{a['status']:<10} {a.get('attempt') or '-':>3} "
              f"{branch:<22} "
              f"{a.get('dirty_files') if a.get('dirty_files') is not None else '-':>5} "
              f"{secs:>7.1f} {cost_s}{why}")

    total_cost = summary.get("total_cost_usd") or 0.0
    lead = "" if summary.get("cost_is_complete", True) else "≥"
    print(f"\n{summary['ok']}/{summary['total']} ok"
          f"（status=completed 的有 {summary['completed']} 个）"
          f"  {lead}${total_cost:.4f}  ->  {paths.dir}")
    return 0 if summary["status"] == P.COMPLETED else 1


def cmd_prune(args: argparse.Namespace) -> int:
    """回收某次 run 留下的 worktree。

    worktree 不回收就会永久留在 .git/worktrees 里。而 runs/ 是 gitignore 的，
    用户手工删掉目录后注册信息还在，git 会一直报 prunable —— 所以要有正规出口。
    分支默认保留：里面是 Agent 的劳动成果。
    """
    run_dir = Path(args.run_dir).resolve()
    n = 0
    for path in sorted((run_dir / "agents").glob("*.result.json")):
        doc = P.read_json(path) or {}
        ws = doc.get("workspace") or {}
        if ws.get("kind") != W.WORKTREE or not ws.get("path"):
            continue
        obj = W.Workspace(
            agent_id=doc.get("agent_id", path.stem),
            kind=W.WORKTREE,
            path=Path(ws["path"]),
            repo=Path(ws.get("repo") or REPO_ROOT),
            branch=ws.get("branch"),
            base_ref=ws.get("base_ref"),
            head=ws.get("head_at_start"),
        )
        # worktree remove 必须 --force 才能删脏工作区，而 --force 会连未提交的
        # 改动一起删。默认拒绝，别让回收动作静默销毁 Agent 的成果。
        if W.is_dirty(obj) and not args.force:
            print(f"{obj.agent_id:<14} 拒绝：工作区有未提交改动，"
                  f"先提交或加 --force")
            n += 1
            continue
        print(f"{obj.agent_id:<14} {W.remove(obj, delete_branch=args.delete_branches)}")
        n += 1
    print(f"\n处理 {n} 个 worktree"
          f"{'，分支已一并删除' if args.delete_branches else '，分支保留'}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    for path in sorted((run_dir / "agents").glob("*.result.json")):
        doc = P.read_json(path) or {}
        verdict = doc.get("verdict") or {}
        mark = "OK" if verdict.get("ok") else f"FAIL({'+'.join(verdict.get('reasons') or ['?'])})"
        print("=" * 72)
        print(f"{doc.get('agent_id')}  {mark}  status={doc.get('status')}  "
              f"attempt={doc.get('attempt')}  session={doc.get('session_id')}")
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
    p_run.add_argument("--max-parallel", type=int, default=3,
                       help="并行上限，plan 里的 max_parallel 优先")
    p_run.add_argument("--retries", type=int, default=None,
                       help="额外重试次数，压过 plan 的 run 级默认；task 级仍优先")
    p_run.add_argument("--cleanup", action="store_true",
                       help="收尾时移除 worktree（保留分支）")
    p_run.add_argument("--no-commit", dest="autocommit", action="store_false",
                       help="不自动把 Agent 的改动提交到它的分支上")
    p_run.add_argument("--forward-anthropic", action="store_true",
                       help="排障用：把 ANTHROPIC_*/CLAUDE_* 转发进子进程环境")
    p_run.set_defaults(func=cmd_run)

    p_show = sub.add_parser("show", help="打印某次 run 的各 Agent 回答")
    p_show.add_argument("run_dir")
    p_show.set_defaults(func=cmd_show)

    p_prune = sub.add_parser("prune", help="回收某次 run 留下的 worktree")
    p_prune.add_argument("run_dir")
    p_prune.add_argument("--force", action="store_true",
                         help="即使工作区有未提交改动也删除（会丢失这些改动）")
    p_prune.add_argument("--delete-branches", action="store_true",
                         help="连同分支一起删除（会丢失 Agent 的改动）")
    p_prune.set_defaults(func=cmd_prune)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
