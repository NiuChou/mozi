"""router 全分支白盒 (detect_task_type/_resolve_policy/_candidate_pool/_score/route) + scorers 单测。"""
from __future__ import annotations

import unittest

try:  # ★先触发临时库环境 (虽不用 conn, 保证 mozi_backend import 前 env 已设)
    from .._helpers import fresh_conn  # noqa: F401
except ImportError:
    try:
        from tests._helpers import fresh_conn  # noqa: F401
    except ImportError:
        from _helpers import fresh_conn  # noqa: F401

from mozi_backend.gateway.models import MODELS  # noqa: E402
from mozi_backend.gateway.router import (  # noqa: E402
    RouteRequest,
    _candidate_pool,
    _resolve_policy,
    _score,
    detect_task_type,
    route,
)

from . import scorers  # noqa: E402


def _req(**kw) -> RouteRequest:
    return RouteRequest(**kw)


class DetectTaskTypeTest(unittest.TestCase):
    def test_long_overrides_code(self):
        self.assertEqual(detect_task_type("```def f()```", 300000), "long")

    def test_code(self):
        self.assertEqual(detect_task_type("def f(): pass", 1000), "code")
        self.assertEqual(detect_task_type("SELECT * FROM t", 1000), "code")

    def test_general_and_empty(self):
        self.assertEqual(detect_task_type("今天天气", 1000), "general")
        self.assertEqual(detect_task_type("", 1000), "general")


class ResolvePolicyTest(unittest.TestCase):
    def test_explicit_kept(self):
        self.assertEqual(_resolve_policy(_req(policy="economy"), "general"), "economy")

    def test_illegal_falls_auto(self):
        self.assertEqual(_resolve_policy(_req(policy="nonsense"), "code"), "code")

    def test_auto_task_driven(self):
        self.assertEqual(_resolve_policy(_req(policy="auto"), "code"), "code")
        self.assertEqual(_resolve_policy(_req(policy="auto"), "long"), "long_context")

    def test_auto_budget_economy(self):
        self.assertEqual(_resolve_policy(_req(policy="auto", budget_cny=0.01), "general"), "economy")

    def test_auto_default_balanced(self):
        self.assertEqual(_resolve_policy(_req(policy="auto"), "general"), "balanced")


class CandidatePoolTest(unittest.TestCase):
    def test_sovereign_all_domestic(self):
        pool = _candidate_pool(_req(privacy_tier="sovereign"))
        self.assertTrue(all(m.domestic for m in pool))
        ids = {m.id for m in pool}
        self.assertNotIn("claude", ids)
        self.assertNotIn("gpt", ids)

    def test_non_sovereign_full(self):
        self.assertEqual(len(_candidate_pool(_req())), len(MODELS))


class ScoreTest(unittest.TestCase):
    def test_context_too_small_penalized(self):
        req = _req(est_tokens=100_000)
        w = {"task": 1.0, "cost": 1.0, "context": 0.6, "reasoning": 0.6}
        small = _score(MODELS["llama-local"], req, "general", w)   # 32k 装不下 → -3.0
        big = _score(MODELS["minimax-m3"], req, "general", w)      # 1M 装得下
        self.assertLess(small, big)

    def test_active_provider_relative(self):
        w = {"task": 1.0, "cost": 1.0, "context": 0.6, "reasoning": 0.6}
        on = _req(active_providers={"glm"})
        off = _req(active_providers=set())
        self.assertGreater(_score(MODELS["glm-5.2"], on, "general", w),
                           _score(MODELS["glm-5.2"], off, "general", w),
                           "active 命中相对未命中得分更高 (Δ≈0.85, 断相对不断硬浮点)")


class RouteTest(unittest.TestCase):
    def test_sovereign_chain_domestic(self):
        d = route(_req(privacy_tier="sovereign", text="你好"))
        self.assertTrue(MODELS[d.chosen_model].domestic)
        self.assertTrue(all(MODELS[m].domestic for m in d.fallback_chain))

    def test_budget_forces_flash(self):
        d = route(_req(budget_cny=0.005, text="hi"))
        self.assertEqual(d.chosen_model, "deepseek-v4-flash")

    def test_chain_head_and_dedup(self):
        d = route(_req(text="hello"))
        self.assertEqual(d.fallback_chain[0], d.chosen_model)
        self.assertEqual(len(d.fallback_chain), len(set(d.fallback_chain)))

    def test_metadata_shape(self):
        meta = route(_req(text="x")).to_metadata()
        self.assertEqual(meta["type"], "routing_metadata")
        self.assertTrue(all(isinstance(v, float) for v in meta["scores"].values()))


class ScorersTest(unittest.TestCase):
    def test_recall_and_rr(self):
        self.assertEqual(scorers.recall_at_k([False, True, False]), 1.0)
        self.assertEqual(scorers.recall_at_k([False, False]), 0.0)
        self.assertEqual(scorers.reciprocal_rank([False, True]), 0.5)
        self.assertEqual(scorers.reciprocal_rank([]), 0.0)

    def test_prf1(self):
        self.assertEqual(scorers.prf1({("a",)}, {("a",)}), (1.0, 1.0, 1.0))
        p, r, _ = scorers.prf1({("a",), ("b",)}, {("a",), ("c",)})
        self.assertAlmostEqual(p, 0.5)
        self.assertAlmostEqual(r, 0.5)
        self.assertEqual(scorers.prf1(set(), set()), (0.0, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
