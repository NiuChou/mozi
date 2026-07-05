"""UMA 路由引擎 (§8.1 图9 决策流)。v0.1 = 规则前置 + 轻量打分 (LLM-as-Router 兜底)。

一句话: 把『选模型』从用户手里接过来 —— 按策略/隐私/上下文/任务/预算打分选最优,
失败按降级链 (图11) 走。这是 UMA 专利锚点。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import MODELS, ModelSpec, build_fallback_chain

# 6 预设策略 (§7.1 policy)
POLICIES = ["balanced", "quality", "economy", "speed", "code", "long_context"]

# 策略对各信号的权重 (task / cost / context / reasoning)
_POLICY_WEIGHTS: dict[str, dict[str, float]] = {
    "balanced":     {"task": 1.0, "cost": 1.0, "context": 0.6, "reasoning": 0.6},
    "quality":      {"task": 1.0, "cost": 0.2, "context": 0.6, "reasoning": 1.6},
    "economy":      {"task": 0.6, "cost": 2.2, "context": 0.4, "reasoning": 0.2},
    "speed":        {"task": 0.6, "cost": 1.6, "context": 0.3, "reasoning": 0.2},
    "code":         {"task": 1.8, "cost": 0.6, "context": 0.6, "reasoning": 1.0},
    "long_context": {"task": 0.8, "cost": 0.4, "context": 2.4, "reasoning": 0.6},
}

_CODE_RE = re.compile(r"```|\bdef \b|\bclass \b|\bfunction\b|\bimport \b|\b(SELECT|INSERT|CREATE)\b|{|}|;", re.I)


@dataclass
class RouteRequest:
    policy: str = "auto"
    privacy_tier: str = "local_first"   # local_first / cloud / sovereign
    est_tokens: int = 1000
    budget_cny: float | None = None
    text: str = ""
    active_providers: set[str] = field(default_factory=set)


@dataclass
class RoutingDecision:
    chosen_model: str
    strategy: str
    fallback_chain: list[str]
    privacy_tier: str
    reason: str
    task_type: str
    scores: dict[str, float]

    def to_metadata(self, fallback_used: bool = False) -> dict[str, Any]:
        return {
            "type": "routing_metadata",
            "chosen_model": self.chosen_model,
            "strategy": self.strategy,
            "fallback_used": fallback_used,
            "fallback_chain": self.fallback_chain,
            "privacy_tier": self.privacy_tier,
            "task_type": self.task_type,
            "reason": self.reason,
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
        }


def detect_task_type(text: str, est_tokens: int) -> str:
    if est_tokens > 256_000:
        return "long"
    if _CODE_RE.search(text or ""):
        return "code"
    return "general"


def _resolve_policy(req: RouteRequest, task_type: str) -> str:
    if req.policy and req.policy != "auto" and req.policy in _POLICY_WEIGHTS:
        return req.policy
    # auto: 任务驱动
    if task_type == "code":
        return "code"
    if task_type == "long":
        return "long_context"
    if req.budget_cny is not None and req.budget_cny < 0.02:
        return "economy"
    return "balanced"


def _candidate_pool(req: RouteRequest) -> list[ModelSpec]:
    pool = list(MODELS.values())
    # 隐私级硬过滤 (图9: 信创→仅国产A级, 禁外部API)
    if req.privacy_tier == "sovereign":
        pool = [m for m in pool if m.domestic]
    return pool


def _score(m: ModelSpec, req: RouteRequest, task_type: str, weights: dict[str, float]) -> float:
    score = 0.0
    # 任务匹配
    if task_type == "code" and "code" in m.strengths:
        score += 2.0 * weights["task"]
    elif task_type == "long" and "long" in m.strengths:
        score += 2.0 * weights["task"]
    elif task_type == "general" and "general" in m.strengths:
        score += 1.0 * weights["task"]
    # 成本 (越便宜越高分)
    cost = m.price_in + m.price_out
    score += weights["cost"] * (1.0 / (1.0 + cost * 20))
    # 上下文契合 (够用即可, 余量加分但避免浪费)
    if m.context_window >= req.est_tokens:
        score += weights["context"] * min(1.0, m.context_window / 1_000_000)
    else:
        score -= 3.0  # 装不下重罚
    # 推理强度
    if "reasoning" in m.strengths:
        score += weights["reasoning"]
    # 长上下文超长时避让 Kimi (图9 明示)
    if task_type == "long" and m.id == "kimi-k2.7-code":
        score -= 1.5
    # 国产优先轻微加权 (本地优先叙事)
    if m.domestic:
        score += 0.1
    # provider 在线 (有 key) 加权: 自动模式优先真实模型而非 mock; 全无 key 时各家同权回退 mock 演示
    if m.provider in req.active_providers:
        score += 0.8
    elif m.provider != "local":
        score -= 0.05
    return score


def route(req: RouteRequest) -> RoutingDecision:
    task_type = detect_task_type(req.text, req.est_tokens)
    policy = _resolve_policy(req, task_type)
    weights = _POLICY_WEIGHTS[policy]
    pool = _candidate_pool(req)

    scores = {m.id: _score(m, req, task_type, weights) for m in pool}
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    chosen = ranked[0][0]

    # 预算紧 → 显式偏向 flash (图9 预算分支)
    if req.budget_cny is not None and req.budget_cny < 0.01 and "deepseek-v4-flash" in scores:
        chosen = "deepseek-v4-flash"

    # 降级链: 以 chosen 起, 接全局链去重, 过隐私过滤 (ADAPTER 统一 build_fallback_chain)
    chain = build_fallback_chain(chosen, domestic_only=req.privacy_tier == "sovereign")

    reason = f"policy={policy} · task={task_type} · privacy={req.privacy_tier} · ~{req.est_tokens}tok"
    return RoutingDecision(chosen, policy, chain, req.privacy_tier, reason, task_type, scores)
