"""配额执行层 (P1-3): 订阅档位 → 月度 token 预算 → allow/degrade/block 决策。

口径: token_budget = 每 usage period(自然月)绝对额度; usage_ledger 按 period_now() 分桶,
跨月自动归零 (load_quota_state 取当期已用)。rate_multiplier 只乘 cost_cny, 不放大预算消耗。
block 用真实已用量 (不可被低估 est 绕过); degrade 用 projected 前瞻软门。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..db import dal
from ..util import period_now
from .adapters.base import compute_cost
from .models import MODELS

DEGRADE_MODEL = "deepseek-v4-flash"   # models.py domestic=True 已确认, sovereign 降级安全
HARD_CAP_RATIO = 1.10


@dataclass(frozen=True)
class QuotaState:
    plan_code: str
    token_budget: int | None
    rate_multiplier: float
    tokens_used: int
    period: str

    @property
    def remaining(self) -> int | None:
        return None if self.token_budget is None else max(0, self.token_budget - self.tokens_used)

    @property
    def exhausted(self) -> bool:
        return self.token_budget is not None and self.tokens_used >= self.token_budget


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    action: str               # allow | degrade | block
    reason: str
    state: QuotaState
    forced_model: str | None


def load_quota_state(conn, user_id: str) -> QuotaState:
    sub = dal.get_active_subscription(conn, user_id)
    used = dal.usage_summary(conn, user_id)["tokens_used"]   # 按 period_now() 分桶 → 跨期自动归零
    if not sub:
        return QuotaState("free_local", None, 1.0, used, period_now())
    return QuotaState(sub["plan_code"], sub["token_budget"], sub["rate_multiplier"] or 1.0,
                      used, period_now())


def check_quota(conn, user_id: str, *, est_tokens: int = 0, privacy_tier: str = "local_first") -> QuotaDecision:
    """est_tokens 默认 0 (无参调用不 TypeError); 须为含注入 grounding+历史的保守上估。

    block 仅看真实已用量 (tokens_used, 绕过免疫); degrade 用 projected 前瞻软门。
    """
    st = load_quota_state(conn, user_id)
    if st.token_budget is None:
        return QuotaDecision(True, "allow", "无预算限制(本地/免费档)", st, None)
    if st.tokens_used >= st.token_budget * HARD_CAP_RATIO:           # 硬上限: 仅看真实已用量
        return QuotaDecision(False, "block", f"超出档位 {st.plan_code} 预算硬上限", st, None)
    projected = st.tokens_used + max(0, est_tokens)
    if st.exhausted or projected > st.token_budget:
        assert MODELS[DEGRADE_MODEL].domestic, "DEGRADE_MODEL 必须 domestic (sovereign 双保险)"
        return QuotaDecision(True, "degrade", f"预算将用尽, 降级至 {DEGRADE_MODEL}", st, DEGRADE_MODEL)
    return QuotaDecision(True, "allow", "预算充足", st, None)


def billed_cost(spec, tokens_in: int, tokens_out: int, mult: float) -> float:
    """计费 = 基础成本 × 档位倍率 (rate_multiplier 只乘 cost, 不放大预算消耗)。"""
    return round(compute_cost(spec, tokens_in, tokens_out) * mult, 6)


def over_hard_cap(conn, user_id: str) -> bool:
    """落账后二次校验: 真实已用量是否超硬上限 (不回滚本次, 下次 check_quota 即 block)。"""
    st = load_quota_state(conn, user_id)
    return st.token_budget is not None and st.tokens_used >= st.token_budget * HARD_CAP_RATIO
