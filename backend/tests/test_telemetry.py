"""EPIC-TELEMETRY: 本地分片轮转 + recent 跨分片 user 隔离 + 可选 PostHog 经 egress.audit。"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

try:
    from ._helpers import TMP_DIR  # noqa: F401  触发临时库环境
except ImportError:
    from _helpers import TMP_DIR  # noqa: F401

from mozi_backend.db import database  # noqa: E402
from mozi_backend.telemetry import events  # noqa: E402


class _Ctx:
    """rebind events.settings (frozen 安全) + 清 handler 缓存; 退出还原。"""

    def __init__(self, **ov) -> None:
        self.ov = ov

    def __enter__(self):
        self._orig = events.settings
        d = self.ov.setdefault("data_dir", Path(tempfile.mkdtemp(prefix="mozi_tel_")))
        events.settings = dataclasses.replace(events.settings, **self.ov)
        events._reset_handlers()
        return Path(d)

    def __exit__(self, *a):
        events._reset_handlers()
        events.settings = self._orig


class _FakeClient:
    calls: list = []

    def __init__(self, **kw) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        _FakeClient.calls.append({"url": url, "json": json})


class LocalShardTest(unittest.TestCase):
    def test_capture_user_sharded_isolation(self) -> None:
        with _Ctx() as tmp:
            events.capture("chat_send", {"x": 1}, user_id="u_a")
            events.capture("chat_send", {"x": 2}, user_id="u_b")
            self.assertTrue(list((tmp / "telemetry" / "u_a").glob("*.jsonl")))
            a = events.recent(user_id="u_a")
            self.assertTrue(a and all(e["user_id"] == "u_a" for e in a))
            self.assertFalse(any(e["user_id"] == "u_b" for e in a), "物理隔离: u_a 不见 u_b")

    def test_recent_reverse_increment(self) -> None:
        with _Ctx():
            for i in range(1000):
                events.capture("e", {"i": i}, user_id="u")
            r = events.recent(limit=10, user_id="u")
            self.assertEqual(len(r), 10)
            self.assertEqual(r[0]["props"]["i"], 999, "新→旧, 首条为最新")
            self.assertEqual(r[9]["props"]["i"], 990)

    def test_rotation_by_size(self) -> None:
        with _Ctx(telemetry_max_bytes=400, telemetry_max_rolls=3) as tmp:
            for i in range(200):
                events.capture("rot", {"i": i, "pad": "x" * 50}, user_id="u")
            rolls = list((tmp / "telemetry" / "u").glob("*.jsonl.*"))
            self.assertTrue(rolls, "应触发 RotatingFileHandler 轮转生成 .1/.2/.3")
            # backupCount 上限保留最近若干代; recent 跨轮转代新→旧连续回溯
            r = events.recent(limit=5, user_id="u")
            self.assertEqual([e["props"]["i"] for e in r], [199, 198, 197, 196, 195],
                             "recent 跨轮转代新→旧连续回溯")


class PostHogReportTest(unittest.TestCase):
    def setUp(self) -> None:
        _FakeClient.calls = []
        self._orig_client = events.httpx.Client
        events.httpx.Client = _FakeClient
        database.init_db()   # 主库须有 audit_log (egress.audit 经 get_conn 写 settings.db_path)

    def tearDown(self) -> None:
        events.httpx.Client = self._orig_client

    def test_disabled_zero_egress(self) -> None:
        with _Ctx(local_first=True, posthog_key="k"):     # local_first 总闸关 → 不外呼
            events.capture("e", {}, user_id="u")
        self.assertEqual(_FakeClient.calls, [], "local_first 下零外呼 (即便有 key)")

    def test_no_key_zero_egress(self) -> None:
        with _Ctx(local_first=False, posthog_key=None):
            events.capture("e", {}, user_id="u")
        self.assertEqual(_FakeClient.calls, [], "无 key 不外呼")

    def test_enabled_reports_and_audits(self) -> None:
        with _Ctx(local_first=False, posthog_key="phk"):
            events.capture("chat_send", {"p": 1}, user_id="u_rep")
        self.assertTrue(_FakeClient.calls, "key + 非 local_first → 上报")
        self.assertEqual(_FakeClient.calls[0]["json"]["api_key"], "phk")
        self.assertEqual(_FakeClient.calls[0]["json"]["distinct_id"], "u_rep")
        with database.get_conn() as conn:   # 经唯一 egress 门落审计
            row = conn.execute(
                "SELECT egress_flag FROM audit_log WHERE user_id='u_rep' "
                "AND action='telemetry.report'").fetchone()
        self.assertIsNotNone(row, "上报须落一条 telemetry.report 审计")
        self.assertEqual(row[0], 1)

    def test_report_failure_never_raises(self) -> None:
        def _boom(*a, **k):
            raise RuntimeError("network down")
        events.httpx.Client = _boom
        with _Ctx(local_first=False, posthog_key="k") as tmp:
            events.capture("e", {}, user_id="u")   # 不抛
            self.assertTrue(list((tmp / "telemetry" / "u").glob("*.jsonl")), "上报失败本地仍落盘")


if __name__ == "__main__":
    unittest.main()
