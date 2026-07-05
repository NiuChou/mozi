"""EPIC-RETRIEVAL (v3): reindex 切后端后重嵌入 (维度/模型更新 + dry-run no-op)。"""
from __future__ import annotations

import dataclasses
import unittest

try:
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

from mozi_backend.db import dal  # noqa: E402
from mozi_backend.vault import embedder, reindex, service  # noqa: E402


class ReindexTest(unittest.TestCase):
    def tearDown(self) -> None:
        embedder._reset_backend()

    def test_reindex_dry_run_noop(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            service.archive_document(conn, user_id="u", title="t", content="墨子 UMA Vault")
            before = conn.execute("SELECT dim FROM embeddings LIMIT 1").fetchone()["dim"]
            stats = reindex.reindex_user(conn, "u", dry_run=True)
            self.assertGreater(stats["chunks"], 0)
            after = conn.execute("SELECT dim FROM embeddings LIMIT 1").fetchone()["dim"]
            self.assertEqual(before, after, "dry-run 不写库")
        finally:
            conn.close()

    def test_reindex_fts_idempotent(self) -> None:
        """回归: fts_upsert 须幂等 —— 两次 reindex 后每 chunk 仅一条 chunks_fts 行 (旧 bug 会追加重复行, 污染 BM25)。"""
        conn = fresh_conn()
        try:
            if conn.execute("SELECT name FROM sqlite_master WHERE name='chunks_fts'").fetchone() is None:
                self.skipTest("FTS5 不可用, 跳过")
            dal.ensure_user(conn, "u", "u@x.cn")
            service.archive_document(conn, user_id="u", title="t", content="墨子 UMA Vault")
            reindex.reindex_user(conn, "u")
            reindex.reindex_user(conn, "u")
            fts = conn.execute("SELECT chunk_id, COUNT(*) c FROM chunks_fts GROUP BY chunk_id").fetchall()
            self.assertTrue(fts, "应至少一条倒排行")
            self.assertTrue(all(r["c"] == 1 for r in fts), "每 chunk 仅一条 chunks_fts 行")
        finally:
            conn.close()

    def test_reindex_switches_model_and_dim(self) -> None:
        conn = fresh_conn()
        orig = embedder.settings
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            service.archive_document(conn, user_id="u", title="t", content="墨子 UMA 路由网关")
            self.assertEqual(conn.execute("SELECT dim FROM embeddings LIMIT 1").fetchone()["dim"], 256)
            # 切后端: mock 维度 256→128
            embedder.settings = dataclasses.replace(orig, embed_dim=128)
            embedder._reset_backend()
            stats = reindex.reindex_user(conn, "u")
            self.assertEqual(stats["new_dim"], 128)
            dims = {r["dim"] for r in conn.execute("SELECT dim FROM embeddings").fetchall()}
            self.assertEqual(dims, {128}, "reindex 后全部维度更新")
            # 重嵌入后仍可召回
            res = retrieval_search(conn, "u", "UMA 路由网关")
            self.assertTrue(res.hits)
        finally:
            conn.close()
            embedder.settings = orig
            embedder._reset_backend()


def retrieval_search(conn, uid, q):
    from mozi_backend.vault import retrieval
    return retrieval.search(conn, uid, q, routes=["dense"], k=3)


if __name__ == "__main__":
    unittest.main()
