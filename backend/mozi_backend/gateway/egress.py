"""中央出网 choke (P1-4): 唯一受控出网门。egress=True 字面只准出现在本模块。

不出网不记 infer 行 (mock/local 只 events 软登记, 不写 audit_log) —— 与现状逐字一致。
sovereign 硬过滤: 非国产 provider 出网 allowed=False (信创数据主权)。
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass

from ..db import dal
from ..telemetry import events

EGRESS_ACTIONS = {"model.infer", "skill.infer", "tool.web_search", "telemetry.report"}


@dataclass(frozen=True)
class EgressVerdict:
    allowed: bool
    egress: bool
    reason: str


def classify(*, provider: str, privacy_tier: str, is_real: bool) -> EgressVerdict:
    if not is_real or provider == "local":
        return EgressVerdict(True, False, "本地/mock 不出网")
    from ..config import settings
    from .models import MODELS
    # web_search 仅在配了自托管引擎 (SEARXNG_URL) 时才算国产/主权可控;
    # 否则默认后端 (ddgs/DDG 爬虫) 会把 query 外发境外, sovereign 须拦。
    domestic = (any(m.provider == provider and m.domestic for m in MODELS.values())
                or (provider == "web_search" and bool(settings.searxng_url)))
    if privacy_tier == "sovereign" and not domestic:
        return EgressVerdict(False, True, f"sovereign 禁止非国产 provider={provider} 出网")
    return EgressVerdict(True, True, "受控出网")


def audit(conn, *, user_id: str, provider: str, action: str, resource: str,
          privacy_tier: str = "local_first", is_real: bool = True) -> EgressVerdict:
    """唯一记账入口 (同步, 调用方 transaction 内)。

    仅 v.egress 为真才写 audit_log; egress=False(mock/local) 只 events 软登记, 不新增 infer 行
    → mock chat 不产生 model.infer(egress=0) 行 (test_mock_chat_no_egress 继续绿)。
    """
    v = classify(provider=provider, privacy_tier=privacy_tier, is_real=is_real)
    if v.egress:
        dal.log_audit(conn, user_id=user_id, action=action, resource=resource,
                      egress=True, via_guard=True)
    events.capture("egress", {"provider": provider, "action": action, "egress": v.egress,
                              "allowed": v.allowed, "reason": v.reason,
                              "known_action": action in EGRESS_ACTIONS}, user_id)
    return v


@contextlib.asynccontextmanager
async def guard(conn, *, user_id: str, provider: str, action: str, resource: str,
                privacy_tier: str = "local_first", is_real: bool = True):
    """IO+审计同段 (预留)。finally 强制 audit (含异常路径)。"""
    v = classify(provider=provider, privacy_tier=privacy_tier, is_real=is_real)
    try:
        yield v
    finally:
        audit(conn, user_id=user_id, provider=provider, action=action, resource=resource,
              privacy_tier=privacy_tier, is_real=is_real)
