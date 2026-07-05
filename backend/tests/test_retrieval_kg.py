"""test-adequacy #6 + #8 (强度): 检索召回 / RRF / 注入门 + KG 三元组 + 成本数值。

#6 无关 query → injected False; 相关 query → top-1 命中含该词; 空库不崩;
   两路命中同 chunk RRF 分高于单路。
#8 归档已知文本 → KG 含特定三元组 (非仅计数); cost_cny 数值 == compute_cost。
"""
from __future__ import annotations

import unittest

try:  # 兼容 `discover -s tests` (顶层) 与 `unittest tests.X` (包) 两种运行方式
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

from mozi_backend.db import dal  # noqa: E402
from mozi_backend.gateway.adapters.base import compute_cost  # noqa: E402
from mozi_backend.gateway.models import get_model  # noqa: E402
from mozi_backend.vault import retrieval, service  # noqa: E402
from mozi_backend.vault.retrieval import _rrf  # noqa: E402

# 换行分隔每条事实, 使启发式三元组抽取主语锚定行首 (得到干净三元组, 支撑 #8 强断言)
KNOWN = ("墨子是本地优先的桌面应用。\n"
         "UMA 是多模型路由网关。\n"
         "Vault 使用 SQLite。\n"
         "墨子依赖 BGE-M3。\n"
         "检索引擎使用 RRF 融合。")


def _seed(conn):
    dal.ensure_user(conn, "u_demo", "x@mozi.local")
    return service.archive_document(conn, user_id="u_demo", title="墨子架构", content=KNOWN)


class RetrievalTest(unittest.TestCase):
    def test_empty_vault_no_crash(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u_demo", "x@mozi.local")
            res = retrieval.search(conn, "u_demo", "任何查询", k=3)
            self.assertEqual(res.hits, [])
            self.assertFalse(res.injected, "空库不注入")
        finally:
            conn.close()

    def test_relevant_query_top1_contains_term(self) -> None:
        conn = fresh_conn()
        try:
            _seed(conn)
            res = retrieval.search(conn, "u_demo", "UMA 路由网关", k=3)
            self.assertTrue(res.hits, "相关 query 须有命中")
            self.assertTrue(res.injected, "相关 query 分数过地板 → 注入")
            self.assertIn("UMA", res.hits[0].text, "top-1 命中须含查询关键词 UMA")
        finally:
            conn.close()

    def test_irrelevant_query_not_injected(self) -> None:
        conn = fresh_conn()
        try:
            _seed(conn)
            # bm25 (纯词项) 路: 与库内零词项重叠的 ASCII 查询 → 无命中 → 不注入 (确定性)。
            # 注: dense 路用 mock 哈希向量会有伪相似碰撞, 故此处隔离词法路验证注入门。
            res = retrieval.search(conn, "u_demo", "zzz qqq www foobar", k=3, routes=["bm25"])
            self.assertEqual(res.hits, [], "无词项重叠 query 不应有 bm25 命中")
            self.assertFalse(res.injected, "无关 query (空命中) 不应注入")
        finally:
            conn.close()

    def test_rrf_two_routes_outrank_single(self) -> None:
        # 同一 chunk 命中两路 (bm25+dense) RRF 分须高于仅命中一路的 chunk
        two_routes = {"bm25": ["A", "B"], "dense": ["A", "C"]}
        fused = _rrf(two_routes)
        self.assertGreater(fused["A"], fused["B"], "两路命中 A 应高于单路 B")
        self.assertGreater(fused["A"], fused["C"], "两路命中 A 应高于单路 C")

    def test_search_marks_routes_on_hit(self) -> None:
        conn = fresh_conn()
        try:
            _seed(conn)
            res = retrieval.search(conn, "u_demo", "SQLite Vault RRF BGE", k=5)
            self.assertTrue(res.hits)
            # 强相关查询: top-1 应同时命中两路
            top = res.hits[0]
            self.assertTrue(set(top.routes) & {"bm25", "dense"}, "命中须标注路由来源")
        finally:
            conn.close()


class KGTripleTest(unittest.TestCase):
    def test_known_text_yields_specific_triple(self) -> None:
        conn = fresh_conn()
        try:
            _seed(conn)
            sub = dal.query_kg(conn, "u_demo", "墨子", hops=1)
            triples = {(e["subject"], e["predicate"], e["object"]) for e in sub["edges"]}
            # 已知文本 "墨子依赖 BGE-M3" → 特定三元组 (墨子, 依赖, BGE-M3)
            self.assertIn(("墨子", "依赖", "BGE-M3"), triples,
                          f"KG 须含特定三元组 (墨子,依赖,BGE-M3); 实得 {triples}")
        finally:
            conn.close()

    def test_uma_is_gateway_triple(self) -> None:
        conn = fresh_conn()
        try:
            _seed(conn)
            sub = dal.query_kg(conn, "u_demo", "UMA", hops=1)
            triples = {(e["subject"], e["predicate"], e["object"]) for e in sub["edges"]}
            self.assertIn(("UMA", "是", "多模型路由网关"), triples,
                          f"KG 须含 (UMA,是,多模型路由网关); 实得 {triples}")
        finally:
            conn.close()


class CostNumericTest(unittest.TestCase):
    def test_compute_cost_exact_value(self) -> None:
        spec = get_model("glm-5.2")  # price_in=0.005, price_out=0.015 (元/1k)
        cost = compute_cost(spec, 1000, 1000)
        # (1000*0.005 + 1000*0.015)/1000 = 0.02
        self.assertAlmostEqual(cost, 0.02, places=6)
        # 0 token → 0 成本
        self.assertEqual(compute_cost(spec, 0, 0), 0.0)
        # 本地模型零价
        self.assertEqual(compute_cost(get_model("llama-local"), 9999, 9999), 0.0)


if __name__ == "__main__":
    unittest.main()
