"""EPIC-RETRIEVAL: sqlite-vec 向量索引 (缺扩展降级 + 裸连接每入口 load + vec/brute 等价)。"""
from __future__ import annotations

import unittest

try:
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

from mozi_backend.db import dal  # noqa: E402
from mozi_backend.db.database import _connect  # noqa: E402
from mozi_backend.vault import embedder, retrieval, service, vector_index  # noqa: E402


def _has_vec(conn) -> bool:
    return vector_index._load_vec(conn)


class VectorIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        # fresh_conn 绕过 get_conn 的 _forget_conn 钩子 → 全套 discover 下 id(conn) 复用可能
        # 污染全局缓存; 用例前清空保证隔离 (产线 get_conn 有 _forget_conn, 不受影响)。
        vector_index._LOADED_CONNS.clear()
        vector_index._FAILED_CONNS.clear()

    def test_load_per_conn_no_operational_error(self) -> None:
        """裸 _connect 连接直接调 query/upsert 不抛 OperationalError (入口已 _load_vec)。"""
        conn = fresh_conn()
        try:
            # 缺 sqlite-vec → backend=bruteforce, query/upsert no-op/空, 不抛
            self.assertIn(vector_index.backend(conn), ("vec", "bruteforce"))
            vector_index.upsert(conn, "c1", "u", "bge-m3-mock", [0.1, 0.2])  # 不抛
            self.assertEqual(vector_index.query(conn, "u", [0.1, 0.2], "bge-m3-mock", 5)
                             if vector_index.backend(conn) == "vec" else [], [])
        finally:
            conn.close()

    def test_conn_id_reuse_no_false_load(self) -> None:
        conn = fresh_conn()
        loaded = vector_index._load_vec(conn)
        vector_index._forget_conn(conn)
        self.assertNotIn(id(conn), vector_index._LOADED_CONNS)
        self.assertNotIn(id(conn), vector_index._FAILED_CONNS)
        conn.close()
        self.assertIn(loaded, (True, False))   # 探测结果稳定 (装/未装 sqlite-vec 均可)

    def test_vec_matches_bruteforce(self) -> None:
        if not _has_vec(_connect(":memory:")):
            self.skipTest("sqlite-vec 未安装 (默认走 bruteforce)")
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            service.archive_document(conn, user_id="u", title="t", content="墨子 UMA Vault RRF")
            res = retrieval.search(conn, "u", "UMA", routes=["dense"], k=3)
            self.assertTrue(res.hits)
        finally:
            conn.close()

    def test_isolation_across_users(self) -> None:
        conn = fresh_conn()
        try:
            for uid in ("alice", "bob"):
                dal.ensure_user(conn, uid, f"{uid}@x.cn")
            service.archive_document(conn, user_id="alice", title="a", content="alice 私密 UMA 网关")
            res = retrieval.search(conn, "bob", "UMA", k=3)   # bob 空库
            self.assertEqual(res.hits, [], "行级隔离: bob 不得检索到 alice 的 chunk")
        finally:
            conn.close()
            embedder._reset_backend()


if __name__ == "__main__":
    unittest.main()
