"""EPIC-METER: HTTP 配额 (非流式真 429 / 流式首帧 SSE error / GET /v1/quota)。

默认单用户模式 (u_demo, lifespan seed personal_max5=5M)。把已用量顶到硬上限验证 block 行为。
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

try:
    from ._helpers import parse_sse
except ImportError:
    from _helpers import parse_sse

from mozi_backend.config import settings  # noqa: E402
from mozi_backend.db import dal  # noqa: E402
from mozi_backend.db.database import get_conn  # noqa: E402
from mozi_backend.main import app  # noqa: E402


class QuotaHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.client.__enter__()   # lifespan: seed u_demo personal_max5
        with get_conn() as conn:  # 共享库: 清当期用量, 各用例从 0 起 (互不污染)
            conn.execute("DELETE FROM usage_ledger WHERE user_id=?", (settings.default_user_id,))

    def tearDown(self) -> None:
        with get_conn() as conn:
            conn.execute("DELETE FROM usage_ledger WHERE user_id=?", (settings.default_user_id,))
        self.client.__exit__(None, None, None)

    def _set_used(self, tokens: int) -> None:
        with get_conn() as conn:
            dal.bump_usage(conn, settings.default_user_id, tokens, 0.0)

    def test_non_stream_block_returns_429(self) -> None:
        self._set_used(6_000_000)   # > 1.1×5M
        r = self.client.post("/v1/chat", json={
            "messages": [{"role": "user", "content": "墨子用什么数据库？"}],
            "stream": False, "inject_context": False})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["code"], "quota_exceeded")

    def test_stream_block_sse_error_http_200(self) -> None:
        self._set_used(6_000_000)
        r = self.client.post("/v1/chat", json={
            "messages": [{"role": "user", "content": "墨子用什么数据库？"}],
            "stream": True, "inject_context": False})
        self.assertEqual(r.status_code, 200, "流式首帧后 HTTP 已 200, 超限只能走 SSE error")
        evts = parse_sse(r.text)
        err = next((e for e in evts if e.get("type") == "error"), None)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "quota_exceeded")
        self.assertEqual(err["http_status_hint"], 429)

    def test_quota_endpoint_reports_over_hard_cap(self) -> None:
        self._set_used(6_000_000)
        q = self.client.get("/v1/quota").json()
        self.assertEqual(q["plan_code"], "personal_max5")
        self.assertTrue(q["over_hard_cap"])
        self.assertEqual(q["remaining"], 0)

    def test_under_budget_chat_succeeds(self) -> None:
        q = self.client.get("/v1/quota").json()
        self.assertFalse(q["over_hard_cap"])
        r = self.client.post("/v1/chat", json={
            "messages": [{"role": "user", "content": "你好"}],
            "stream": True, "inject_context": False})
        evts = parse_sse(r.text)
        self.assertTrue(any(e.get("type") == "done" for e in evts), "预算内 chat 须正常完成")


if __name__ == "__main__":
    unittest.main()
