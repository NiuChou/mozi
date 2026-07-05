"""种子数据: 档位 plans + demo 用户 + 订阅 (§10 支付与 Token 管理)。"""
from __future__ import annotations

import sqlite3

from ..config import settings
from ..util import jdump, new_id, now
from . import dal

# 墨子档位 (参考 Claude 订阅模型, §10)
PLANS = [
    ("personal_pro", "Personal Pro", 140, 1_000_000, 1.0, {"priority": "base"}),
    ("personal_max5", "Personal Max 5x", 700, 5_000_000, 5.0, {"priority": "high"}),
    ("personal_max20", "Personal Max 20x", 1400, 20_000_000, 20.0, {"priority": "highest"}),
    ("team", "Team / Business", 210, 3_000_000, 3.0, {"seats": True}),
]


def seed(conn: sqlite3.Connection) -> None:
    for code, name, price, budget, mult, feat in PLANS:
        conn.execute(
            "INSERT OR IGNORE INTO plans(plan_code,name,price_cny,token_budget,rate_multiplier,features) VALUES(?,?,?,?,?,?)",
            (code, name, price, budget, mult, jdump(feat)),
        )
    dal.ensure_user(conn, settings.default_user_id, settings.default_user_email, settings.default_region)
    existing = conn.execute(
        "SELECT 1 FROM subscriptions WHERE user_id=?", (settings.default_user_id,)
    ).fetchone()
    if not existing:
        conn.execute(
            """INSERT INTO subscriptions(sub_id,user_id,plan_code,status,period_start,period_end,seats)
               VALUES(?,?,?,?,?,?,?)""",
            (new_id("sub"), settings.default_user_id, "personal_max5", "active", now(), None, 1),
        )
