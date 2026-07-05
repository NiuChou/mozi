"""纯函数打分器 (无 I/O / 无 conn / 无业务 import)。domestic 判定由调用方传 domestic_map。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RoutingMetrics:
    n: int
    task_acc: float
    policy_acc: float
    model_acc: float
    sovereign_ok: float


@dataclass
class RetrievalMetrics:
    n: int
    recall_at_k: float
    mrr: float
    inject_precision: float


@dataclass
class KGMetrics:
    n: int
    precision: float
    recall: float
    f1: float


def _ratio(hit: int, total: int) -> float:
    return hit / total if total else 1.0   # 无该类样本 → 视为满分 (不拉低)


def score_routing(cases: list[dict], decisions: list, domestic_map: dict[str, bool]) -> RoutingMetrics:
    """每指标分母 = 声明了该 expect 字段的样本数 (未声明不计入)。"""
    t_hit = t_tot = p_hit = p_tot = m_hit = m_tot = s_hit = s_tot = 0
    for c, d in zip(cases, decisions):
        exp = c.get("expect", {})
        if "task_type" in exp:
            t_tot += 1
            t_hit += int(d.task_type == exp["task_type"])
        if "policy" in exp:
            p_tot += 1
            p_hit += int(d.strategy == exp["policy"])
        if "models" in exp:
            m_tot += 1
            m_hit += int(d.chosen_model in exp["models"])
        if exp.get("domestic_only"):
            s_tot += 1
            chain_ok = all(domestic_map.get(m, False) for m in d.fallback_chain)
            s_hit += int(domestic_map.get(d.chosen_model, False) and chain_ok)
    return RoutingMetrics(len(cases), _ratio(t_hit, t_tot), _ratio(p_hit, p_tot),
                          _ratio(m_hit, m_tot), _ratio(s_hit, s_tot))


def recall_at_k(relevant_hit: list[bool]) -> float:
    return 1.0 if any(relevant_hit) else 0.0


def reciprocal_rank(relevant_hit: list[bool]) -> float:
    for i, h in enumerate(relevant_hit):
        if h:
            return 1.0 / (i + 1)
    return 0.0


def prf1(expected: set, got: set) -> tuple[float, float, float]:
    tp = len(expected & got)
    p = tp / len(got) if got else 0.0
    r = tp / len(expected) if expected else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f1
