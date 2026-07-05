"""EPIC-FRONTEND: per-message 元数据持久化 + Last-Event-ID 续传尾态回放 (零外呼/不重计费)。"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

try:
    from ._helpers import parse_sse
except ImportError:
    from _helpers import parse_sse

from mozi_backend.db import dal  # noqa: E402
from mozi_backend.db.database import get_conn  # noqa: E402
from mozi_backend.main import app  # noqa: E402


class ChatResumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.client.__enter__()   # 默认单用户 u_demo

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def _chat(self, text="墨子用什么数据库？", headers=None, session_id=None):
        body = {"messages": [{"role": "user", "content": text}], "stream": True,
                "inject_context": False}
        if session_id:
            body["session_id"] = session_id
        return self.client.post("/v1/chat", json=body, headers=headers or {})

    def test_metadata_persisted_and_hits_text_stripped(self) -> None:
        r = self._chat(text="墨子 使用 SQLite。")
        evts = parse_sse(r.text)
        sid = next(e["session_id"] for e in evts if e.get("type") == "session")
        with get_conn() as conn:
            rows = dal.list_messages(conn, sid)
        asst = [m for m in rows if m["role"] == "assistant"][-1]
        self.assertIsNotNone(asst["routing_meta"], "assistant 须落 routing_meta")
        self.assertIsNotNone(asst["usage_meta"], "assistant 须落 usage_meta")
        # inject_context=False → injected=0
        self.assertEqual(asst["injected"], 0)

    def test_resume_replays_tail_without_new_model_call(self) -> None:
        r = self._chat()
        evts = parse_sse(r.text)
        sid = next(e["session_id"] for e in evts if e.get("type") == "session")
        with get_conn() as conn:
            calls_before = conn.execute(
                "SELECT count(*) FROM model_calls WHERE user_id='u_demo'").fetchone()[0]
            msgs_before = len(dal.list_messages(conn, sid))
        # 重连: 带 Last-Event-ID + 同 session → 回放尾态, 不触发 adapter
        r2 = self._chat(headers={"Last-Event-ID": "5"}, session_id=sid)
        evts2 = parse_sse(r2.text)
        self.assertTrue(any(e.get("type") == "done" for e in evts2), "续传须回放到 done")
        self.assertTrue(any(e.get("type") == "delta" for e in evts2), "续传须回放 delta 尾态")
        with get_conn() as conn:
            calls_after = conn.execute(
                "SELECT count(*) FROM model_calls WHERE user_id='u_demo'").fetchone()[0]
            msgs_after = len(dal.list_messages(conn, sid))
        self.assertEqual(calls_after, calls_before, "回放不得新增 model_call (零外呼/不重计费)")
        self.assertEqual(msgs_after, msgs_before, "回放不得新增消息")

    def test_no_last_event_id_runs_normally(self) -> None:
        r = self._chat()
        evts = parse_sse(r.text)
        sid = next(e["session_id"] for e in evts if e.get("type") == "session")
        # 无 Last-Event-ID + 同 session → 正常 run_chat (不回放), 新增一轮对话
        with get_conn() as conn:
            before = len(dal.list_messages(conn, sid))
        self._chat(session_id=sid)
        with get_conn() as conn:
            after = len(dal.list_messages(conn, sid))
        self.assertGreater(after, before, "无续传头应正常追加对话")


if __name__ == "__main__":
    unittest.main()
