"""test-adequacy #7: 多用户隔离 + 并发。

- MOZI_MULTIUSER=1: alice 归档实体 X, bob 检索/KG 查 X → bob 空 (行级隔离)。
- asyncio.gather 并发 N 次 chat → 无异常且 usage.requests == N。
"""
from __future__ import annotations

import asyncio
import unittest

try:  # 兼容 `discover -s tests` (顶层) 与 `unittest tests.X` (包) 两种运行方式
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

import mozi_backend.gateway.orchestrator as orch  # noqa: E402
from mozi_backend.db import dal  # noqa: E402
from mozi_backend.vault import retrieval, service  # noqa: E402

SECRET = "阿尔法机密项目代号 Xylophone 仅 alice 可见。\nalice 拥有 Xylophone。"


class IsolationTest(unittest.TestCase):
    def test_vault_search_isolated_per_user(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "alice", "a@mozi.local")
            dal.ensure_user(conn, "bob", "b@mozi.local")
            service.archive_document(conn, user_id="alice", title="机密", content=SECRET)

            alice_hits = retrieval.search(conn, "alice", "Xylophone", k=5)
            bob_hits = retrieval.search(conn, "bob", "Xylophone", k=5)
            self.assertTrue(alice_hits.hits, "alice 须能检索到自己的文档")
            self.assertEqual(bob_hits.hits, [], "bob 不得检索到 alice 的文档 (行级隔离)")
        finally:
            conn.close()

    def test_kg_query_isolated_per_user(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "alice", "a@mozi.local")
            dal.ensure_user(conn, "bob", "b@mozi.local")
            service.archive_document(conn, user_id="alice", title="机密", content=SECRET)

            alice_kg = dal.query_kg(conn, "alice", "Xylophone")
            bob_kg = dal.query_kg(conn, "bob", "Xylophone")
            self.assertTrue(alice_kg["nodes"], "alice KG 须含 Xylophone 实体")
            self.assertEqual(bob_kg["nodes"], [], "bob KG 不得见 alice 的实体")
            self.assertEqual(bob_kg["edges"], [])
        finally:
            conn.close()

    def test_export_scoped_to_user(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "alice", "a@mozi.local")
            dal.ensure_user(conn, "bob", "b@mozi.local")
            service.archive_document(conn, user_id="alice", title="机密", content=SECRET)
            bob_export = dal.export_user_data(conn, "bob")
            self.assertEqual(bob_export["vault_documents"], [], "bob 导出不含 alice 文档")
            self.assertEqual(bob_export["kg_entities"], [])
            alice_export = dal.export_user_data(conn, "alice")
            self.assertTrue(alice_export["vault_documents"], "alice 导出含自己文档")
        finally:
            conn.close()


class ConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    async def test_gather_n_chats_no_error_requests_eq_n(self) -> None:
        # 多用户多 session 并发 (各自独立连接, SQLite WAL 支持并发读写)
        n = 6
        from mozi_backend.db.database import _connect, init_db
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(path)

        # 各用户独立连接 (模拟每请求一连接), 同一 user 计数累加
        async def one_chat(idx: int) -> list:
            c = _connect(path)
            try:
                dal.ensure_user(c, "u_demo", "x@mozi.local")
                sid = dal.create_session(c, "u_demo", f"s{idx}", "auto")
                events = []
                async for e in orch.run_chat(
                    conn=c, user_id="u_demo", session_id=sid,
                    user_text=f"并发请求 {idx}", inject_context=False,
                    active_providers=set()):
                    events.append(e)
                return events
            finally:
                c.close()

        results = await asyncio.gather(*[one_chat(i) for i in range(n)], return_exceptions=True)
        for r in results:
            self.assertNotIsInstance(r, Exception, f"并发 chat 不得抛异常: {r}")
        for r in results:
            types = [e["type"] for e in r]
            self.assertIn("done", types, "每路并发须完成")

        # usage.requests 累加须 == N
        c = _connect(path)
        try:
            summary = dal.usage_summary(c, "u_demo")
        finally:
            c.close()
            os.unlink(path)
        self.assertEqual(summary["requests"], n,
                         f"并发 {n} 次 chat 后 usage.requests 须为 {n}, 实得 {summary['requests']}")
        self.assertGreater(summary["tokens_used"], 0)


if __name__ == "__main__":
    unittest.main()
