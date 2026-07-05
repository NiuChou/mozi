"""EPIC-METER: 配额执行层 (allow/degrade/block + 真实已用量硬上限 + 周期归零 + 倍率计费)。"""
from __future__ import annotations

import unittest

try:
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

from mozi_backend.db import dal  # noqa: E402
from mozi_backend.gateway import quota  # noqa: E402
from mozi_backend.gateway.adapters.base import compute_cost  # noqa: E402
from mozi_backend.gateway.models import MODELS, get_model  # noqa: E402
from mozi_backend.util import new_id, now, period_now  # noqa: E402

_5M = 5_000_000


def _seed_sub(conn, uid, *, budget, mult=1.0, period_end=None, plan="plan_x") -> None:
    dal.ensure_user(conn, uid, f"{uid}@x.cn")
    conn.execute("INSERT OR IGNORE INTO plans(plan_code,name,price_cny,token_budget,rate_multiplier) "
                 "VALUES(?,?,?,?,?)", (plan, "P", 100, budget, mult))
    conn.execute("INSERT INTO subscriptions(sub_id,user_id,plan_code,status,period_start,period_end,seats) "
                 "VALUES(?,?,?,?,?,?,1)", (new_id("sub"), uid, plan, "active", now(), period_end))


class QuotaLogicTest(unittest.TestCase):
    def test_no_subscription_unlimited(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            qd = quota.check_quota(conn, "u", est_tokens=10_000)
            self.assertEqual(qd.state.plan_code, "free_local")
            self.assertIsNone(qd.state.token_budget)
            self.assertEqual(qd.action, "allow")
        finally:
            conn.close()

    def test_expired_subscription_falls_back(self) -> None:
        conn = fresh_conn()
        try:
            _seed_sub(conn, "u", budget=_5M, period_end="2000-01-01 00:00:00")  # 已过期
            qd = quota.check_quota(conn, "u")
            self.assertEqual(qd.state.plan_code, "free_local", "过期 active 订阅须降级 free_local")
            self.assertEqual(qd.action, "allow")
        finally:
            conn.close()

    def test_under_budget_allow(self) -> None:
        conn = fresh_conn()
        try:
            _seed_sub(conn, "u", budget=_5M)
            dal.bump_usage(conn, "u", 1_000_000, 0.0)
            self.assertEqual(quota.check_quota(conn, "u", est_tokens=1000).action, "allow")
        finally:
            conn.close()

    def test_check_quota_default_est_no_typeerror(self) -> None:
        conn = fresh_conn()
        try:
            _seed_sub(conn, "u", budget=_5M)
            self.assertEqual(quota.check_quota(conn, "u", privacy_tier="local_first").action, "allow")
        finally:
            conn.close()

    def test_near_exhaust_degrade(self) -> None:
        conn = fresh_conn()
        try:
            _seed_sub(conn, "u", budget=_5M)
            dal.bump_usage(conn, "u", _5M, 0.0)              # used == budget → exhausted
            qd = quota.check_quota(conn, "u", est_tokens=1000)
            self.assertEqual(qd.action, "degrade")
            self.assertEqual(qd.forced_model, "deepseek-v4-flash")
            self.assertTrue(MODELS[qd.forced_model].domestic)
        finally:
            conn.close()

    def test_hard_cap_block_uses_real_usage(self) -> None:
        conn = fresh_conn()
        try:
            _seed_sub(conn, "u", budget=_5M)
            dal.bump_usage(conn, "u", 6_000_000, 0.0)        # > 1.1×5M
            qd = quota.check_quota(conn, "u", est_tokens=0)  # est 故意 0
            self.assertEqual(qd.action, "block")
            self.assertFalse(qd.allowed)
        finally:
            conn.close()

    def test_low_est_cannot_bypass_block(self) -> None:
        conn = fresh_conn()
        try:
            _seed_sub(conn, "u", budget=_5M)
            dal.bump_usage(conn, "u", 6_000_000, 0.0)
            self.assertEqual(quota.check_quota(conn, "u", est_tokens=0).action, "block",
                             "低估 est 不可绕过硬上限 (block 用真实已用量)")
        finally:
            conn.close()

    def test_rate_multiplier_billing(self) -> None:
        spec = get_model("glm-5.2")
        base = compute_cost(spec, 1000, 1000)
        self.assertAlmostEqual(quota.billed_cost(spec, 1000, 1000, 5.0), round(base * 5, 6), places=6)

    def test_sovereign_degrade_stays_domestic(self) -> None:
        conn = fresh_conn()
        try:
            _seed_sub(conn, "u", budget=_5M)
            dal.bump_usage(conn, "u", _5M, 0.0)
            qd = quota.check_quota(conn, "u", est_tokens=1000, privacy_tier="sovereign")
            self.assertEqual(qd.action, "degrade")
            self.assertTrue(MODELS[qd.forced_model].domestic, "sovereign degrade 须国产")
        finally:
            conn.close()

    def test_period_rollover_resets_usage(self) -> None:
        conn = fresh_conn()
        try:
            _seed_sub(conn, "u", budget=_5M)
            # 把已用量记到过去周期 → 当期 usage_summary 归零
            conn.execute("INSERT INTO usage_ledger(entry_id,user_id,period,tokens_used,requests,cost_cny) "
                         "VALUES(?,?,?,?,?,?)", (new_id("u"), "u", "2000-01", 9_000_000, 1, 0.0))
            self.assertEqual(quota.load_quota_state(conn, "u").tokens_used, 0, "跨期当期归零")
            self.assertEqual(quota.check_quota(conn, "u", est_tokens=1000).action, "allow")
            self.assertNotEqual(period_now(), "2000-01")
        finally:
            conn.close()

    def test_over_hard_cap_helper(self) -> None:
        conn = fresh_conn()
        try:
            _seed_sub(conn, "u", budget=_5M)
            dal.bump_usage(conn, "u", 6_000_000, 0.0)
            self.assertTrue(quota.over_hard_cap(conn, "u"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
