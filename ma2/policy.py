"""成败判定与重试策略。

存在的理由是 README 结论 3：**`status == completed` 不足以判定任务成功。**
工具被 `--allowedTools` 挡掉时，Agent 不会挂起，而是解释一番然后正常收尾 ——
`status` 是 completed，`stop_reason` 是 end_turn，但它什么也没干成。

所以这里把两件事分开：

    status   协议层事实。进程和事件流实际发生了什么，由 events.py 归约得出。
    verdict  策略层判断。按当前策略算不算成功，由本模块得出。

不把 verdict 塞回 status，是为了保住审计的准确性 —— "Agent 报告自己完成了"
和"我们认为这次任务成功了"是两个不同的事实，合并就再也分不开了。

本模块纯函数、无 IO。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import protocol as P

# ---------------------------------------------------------------- 失败原因码
TIMEOUT = "timeout"                      # 超时被终止
CRASHED = "crashed"                      # 进程失败，或 result 事件 is_error
PERMISSION_DENIED = "permission_denied"  # 有工具被权限拦下
EMPTY_ANSWER = "empty_answer"            # 跑完了但没给出回答
NO_CHANGES = "no_changes"                # 声明要改文件却一个字节都没动

# 默认可重试集合。
#
# PERMISSION_DENIED 刻意不在里面：它是配置错误不是抖动。同样的 allowed_tools
# 重试多少次都会被同样拦下，纯粹烧钱。这类失败要的是人去改 plan，不是重试。
RETRYABLE = frozenset({TIMEOUT, CRASHED, EMPTY_ANSWER, NO_CHANGES})

DEFAULT_CHECKS: dict[str, bool] = {
    "deny_permission_denials": True,
    "require_answer": True,
    "require_changes": False,  # 只对改代码的任务有意义，按需打开
}


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": self.reasons, "retryable": self.retryable}


def resolve_checks(task: dict[str, Any], run_defaults: dict[str, Any] | None = None) -> dict[str, bool]:
    """任务级 checks 覆盖 run 级，run 级覆盖内置默认。"""
    checks = dict(DEFAULT_CHECKS)
    checks.update((run_defaults or {}).get("checks") or {})
    checks.update(task.get("checks") or {})
    return checks


def evaluate(result: dict[str, Any], *,
             checks: dict[str, bool] | None = None,
             changes: dict[str, Any] | None = None,
             retryable: frozenset[str] = RETRYABLE) -> Verdict:
    """判定单次尝试的成败。

    changes 是 workspace.collect_changes 的返回值，只有 require_changes
    打开时才需要。
    """
    checks = checks if checks is not None else dict(DEFAULT_CHECKS)
    reasons: list[str] = []

    status = result.get("status")
    if status == P.TIMEOUT:
        reasons.append(TIMEOUT)
    elif status != P.COMPLETED:
        reasons.append(CRASHED)

    diagnostics = result.get("diagnostics") or {}
    if checks.get("deny_permission_denials", True):
        if diagnostics.get("permission_denials"):
            reasons.append(PERMISSION_DENIED)

    if checks.get("require_answer", True) and status == P.COMPLETED:
        if not (result.get("answer") or "").strip():
            reasons.append(EMPTY_ANSWER)

    if checks.get("require_changes", False) and status == P.COMPLETED:
        changed = bool(changes) and (
            changes.get("dirty_files") or changes.get("committed")
        )
        if not changed:
            reasons.append(NO_CHANGES)

    if not reasons:
        return Verdict(ok=True)

    # 只要有一个原因不可重试，整体就不重试：重试也解决不了那一条。
    return Verdict(
        ok=False,
        reasons=reasons,
        retryable=all(r in retryable for r in reasons),
    )


def retry_plan(task: dict[str, Any], run_defaults: dict[str, Any] | None = None) -> tuple[int, float, bool]:
    """解析重试配置，返回 (最大尝试次数, 退避基数秒, 是否在重试前重置工作区)。

    retries 是**额外**尝试次数，所以最大尝试次数 = retries + 1。
    """
    defaults = run_defaults or {}
    retries = task.get("retries", defaults.get("retries", 0))
    delay = task.get("retry_delay_sec", defaults.get("retry_delay_sec", 2.0))
    reset = task.get("reset_between_attempts",
                     defaults.get("reset_between_attempts", True))
    return max(1, int(retries) + 1), max(0.0, float(delay)), bool(reset)
