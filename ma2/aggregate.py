"""汇总层。N 份 result.json → 一份 final.md。

本模块的核心决定是**把汇总切成两层**：

    机械层  纯代码。谁成功了、失败原因是什么、改了哪些文件、产出在哪个分支
            上、花了多少钱。免费、确定、可测，**无条件产出**。
    综合层  一个 Agent。把 N 份回答读成一段有观点的正文。要花钱，只有 N 大到
            人不愿意逐份读的时候才划算，因此**显式开启**。

为什么不干脆整层交给一个 Agent：总结器失败是常态之一（超时、限流、跑题、
权限被拦）。如果 final.md 只有 Agent 能写，总结器一挂，整次 run 的成果就只剩
一堆散落的 JSON —— 那正是结论 7「回收动作静默销毁产出」换了个位置重演。
所以综合失败时 final.md 照样写出来，失败原因就写在正文的位置上。

为什么产出只从分支读（见 workspace.read_branch）：工作目录可能已被 --cleanup
回收，分支才是产出的持久所在。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import workspace as W

ANSWER_LIMIT = 4000
FILE_LIMIT = 4000
MAX_FILES = 20

# 汇总 Agent 的默认指令。刻意不谈"总结得漂亮"，只要求它做人不愿意做的那部分：
# 交叉比对 N 份回答，指出分歧和缺口。
DEFAULT_INSTRUCTION = """\
下面是一次多 Agent 并行运行的简报。请把它综合成一份给人读的结论，用中文，
Markdown 格式，不要重复简报里已有的表格和原文。要求：

1. 先给结论：这次运行整体上得到了什么，没得到什么。
2. 指出各 Agent 之间的**分歧与重复**：谁和谁说法不一致，哪些结论互相印证。
3. 失败的 Agent 影响了哪些结论的可信度，明确说出缺口在哪。
4. 不要编造简报里没有的事实；简报里没写的就说没有。"""

DEFAULT_SUFFIX = ("你是被编排器 headless 调用的汇总 agent，最终回复会被直接写进 "
                  "final.md，只输出正文本身，不要前言、不要复述指令。")


def clip(text: str, limit: int) -> tuple[str, bool]:
    text = text or ""
    return (text[:limit], len(text) > limit)


def gather(results: list[dict[str, Any]], tasks: list[dict[str, Any]] | None = None,
           *, max_files: int = MAX_FILES, max_bytes: int = FILE_LIMIT,
           ) -> list[dict[str, Any]]:
    """把每份 result.json 变成汇总用的记录，产出从分支读。"""
    prompts = {t["id"]: t.get("prompt", "") for t in (tasks or [])}
    records: list[dict[str, Any]] = []
    for r in results:
        ws = r.get("workspace") or {}
        verdict = r.get("verdict") or {}
        metrics = r.get("metrics") or {}
        answer, answer_clipped = clip(r.get("answer") or "", ANSWER_LIMIT)
        rec: dict[str, Any] = {
            "agent_id": r.get("agent_id"),
            "prompt": prompts.get(r.get("agent_id"), ""),
            "status": r.get("status"),
            "ok": bool(verdict.get("ok")),
            "reasons": verdict.get("reasons") or [],
            "attempt": r.get("attempt"),
            "answer": answer,
            "answer_clipped": answer_clipped,
            "branch": ws.get("branch"),
            "cost_usd": metrics.get("cost_all_attempts_usd"),
            "cost_is_complete": metrics.get("cost_is_complete", True),
            "duration_ms": (metrics.get("wall_ms_all_attempts")
                            or metrics.get("wall_ms")
                            or metrics.get("duration_ms")),
            "artifacts": None,
        }
        if ws.get("kind") == W.WORKTREE and ws.get("branch") and ws.get("repo"):
            rec["artifacts"] = W.read_branch(
                Path(ws["repo"]), ws["branch"], ws.get("head_at_start"),
                max_files=max_files, max_bytes=max_bytes,
            )
        records.append(rec)
    return records


def _verdict_label(rec: dict[str, Any]) -> str:
    return "OK" if rec["ok"] else f"FAIL({'+'.join(rec['reasons']) or '?'})"


def build_brief(run_id: str, run_name: str | None,
                records: list[dict[str, Any]]) -> str:
    """给汇总 Agent 看的简报。纯字符串拼接，不做 IO，便于单测。

    简报整份走 prompt（stdin），因此汇总 Agent 不需要任何工具就能读到全部
    输入 —— 少一个工具就少一类失败面。同时它也落盘成 brief.md：汇总说错话
    的时候，第一个要查的就是它到底看到了什么。
    """
    ok = sum(1 for r in records if r["ok"])
    lines = [
        f"# 运行简报 {run_id}",
        "",
        f"计划: {run_name or '(未命名)'}",
        f"Agent: {len(records)} 个，判定成功 {ok} 个",
        "",
    ]
    for rec in records:
        lines += [
            "-" * 60,
            f"## {rec['agent_id']}  {_verdict_label(rec)}"
            f"  (status={rec['status']}, 第 {rec['attempt']} 次尝试)",
            "",
        ]
        if rec["prompt"]:
            prompt, clipped = clip(rec["prompt"], 600)
            lines += ["### 它被要求做什么", prompt + ("…（截断）" if clipped else ""), ""]
        lines += ["### 它的回答"]
        lines += [rec["answer"] or "(空)"]
        if rec["answer_clipped"]:
            lines.append("…（回答过长已截断）")
        lines.append("")

        art = rec["artifacts"]
        if art and art.get("error"):
            lines += [f"### 产出：读取分支失败 —— {art['error']}", ""]
        elif art:
            head = f"### 它在分支 {art['branch']} 上的产出（{art['total_files']} 个文件）"
            lines.append(head)
            if art.get("omitted_files"):
                lines.append(f"（只列出前 {len(art['files'])} 个，另有 "
                             f"{art['omitted_files']} 个未列出）")
            for item in art["files"]:
                lines.append(f"\n#### {item['change']}  {item['path']}")
                if item.get("binary"):
                    lines.append("(二进制文件，内容略)")
                elif item.get("error"):
                    lines.append(f"(读取失败: {item['error']})")
                else:
                    lines += ["```", item.get("text", ""), "```"]
                    if item.get("truncated"):
                        lines.append("（文件过长已截断）")
            lines.append("")
        elif rec["ok"]:
            lines += ["### 产出", "（非 worktree 工作区，没有分支产出可读）", ""]
    return "\n".join(lines) + "\n"


def aggregator_task(plan: dict[str, Any], brief: str) -> dict[str, Any]:
    """构造汇总 Agent 的 task。

    它是一个**普通 task**：同一个 run_agent、同一套超时、同一套 policy 判定与
    重试。汇总器没有理由享受特殊待遇 —— 它一样会超时、一样会跑题。
    """
    cfg = plan.get("aggregator")
    cfg = cfg if isinstance(cfg, dict) else {}
    task: dict[str, Any] = {
        "id": cfg.get("id", "aggregator"),
        "agent": cfg.get("agent", "claude"),
        "prompt": f"{cfg.get('prompt') or DEFAULT_INSTRUCTION}\n\n{brief}",
        "timeout_sec": cfg.get("timeout_sec", 300),
        "retries": cfg.get("retries", 1),
        "retry_delay_sec": cfg.get("retry_delay_sec", 2.0),
        "system_suffix": cfg.get("system_suffix", DEFAULT_SUFFIX),
        # 简报已经整份在 prompt 里，汇总不需要读文件；不给工具就少一类失败面
        "allowed_tools": cfg.get("allowed_tools", []),
        "checks": {"require_changes": False, **(cfg.get("checks") or {})},
    }
    for key in ("launcher", "model", "permission_mode"):
        if cfg.get(key) is not None:
            task[key] = cfg[key]
    return task


def _cost(rec: dict[str, Any]) -> str:
    cost = rec.get("cost_usd")
    if not isinstance(cost, (int, float)):
        return "-"
    return f"{'' if rec.get('cost_is_complete', True) else '≥'}${cost:.4f}"


def render_final(run_id: str, run_name: str | None, summary: dict[str, Any],
                 records: list[dict[str, Any]],
                 synthesis: dict[str, Any] | None = None) -> str:
    """产出 final.md。

    synthesis 为 None 表示没开综合层；带 ok=False 表示开了但失败了。两种情况
    都照常出文档，区别只在正文那一节写什么 —— 机械层的价值不依赖 Agent。
    """
    ok = sum(1 for r in records if r["ok"])
    lines = [
        f"# {run_name or run_id} 汇总",
        "",
        f"- run_id: `{run_id}`",
        f"- 完成时间: {summary.get('finished_at', '')}",
        f"- 判定: **{ok}/{len(records)} 个 Agent 成功**"
        f"（其中 status=completed 的有 {summary.get('completed', '?')} 个）",
        "",
        "## 综合",
        "",
    ]

    if synthesis is None:
        lines += ["（未启用综合层。机械汇总见下。开启方式：plan 里加 "
                  "`\"aggregator\": {}`，或 `run --aggregate`。）"]
    elif synthesis.get("ok"):
        lines += [synthesis.get("answer") or ""]
    else:
        why = "+".join(synthesis.get("reasons") or ["?"])
        lines += [f"> **综合层失败：{why}**（status={synthesis.get('status')}，"
                  f"第 {synthesis.get('attempt')} 次尝试）。",
                  ">",
                  "> 下面的机械汇总不受影响 —— 各 Agent 的产出都在自己的分支上，"
                  "没有因为总结失败而丢失。"]
    lines += ["", "## 明细", "",
              "| agent | 判定 | status | 尝试 | 分支 | 改动 | 耗时 | 费用 |",
              "|---|---|---|---|---|---|---|---|"]
    for rec in records:
        art = rec["artifacts"] or {}
        changed = art.get("total_files")
        secs = (rec.get("duration_ms") or 0) / 1000
        branch = f"`{rec['branch']}`" if rec.get("branch") else "-"
        lines.append(
            f"| {rec['agent_id']} | {_verdict_label(rec)} | {rec['status']} "
            f"| {rec['attempt']} | {branch} "
            f"| {changed if changed is not None else '-'} "
            f"| {secs:.1f}s | {_cost(rec)} |"
        )

    lines += ["", "## 各 Agent 回答", ""]
    for rec in records:
        lines += [f"### {rec['agent_id']}  {_verdict_label(rec)}", "",
                  rec["answer"] or "(空)"]
        if rec["answer_clipped"]:
            lines.append("\n（回答过长已截断，完整内容见 "
                         f"`agents/{rec['agent_id']}.result.json`）")
        lines.append("")

    produced = [r for r in records if (r["artifacts"] or {}).get("total_files")]
    if produced:
        lines += ["## 产出在哪", "",
                  "工作区可能已被回收，以下分支是产出的持久所在：", ""]
        for rec in produced:
            art = rec["artifacts"]
            lines.append(f"- `{art['branch']}` —— {art['total_files']} 个文件："
                         + "、".join(f["path"] for f in art["files"][:5])
                         + ("…" if art.get("omitted_files") else ""))
        lines.append("")

    return "\n".join(lines)
