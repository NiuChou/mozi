"""EPIC-KG: N-hop 子图 + 三级消歧 + LLM 抽取真实化 + 出网必留痕 (v2 base + v3 深化)。"""
from __future__ import annotations

import unittest

try:
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

import mozi_backend.gateway.llm as llmmod  # noqa: E402
import mozi_backend.gateway.orchestrator as orch  # noqa: E402
import mozi_backend.vault.kg as kgmod  # noqa: E402
from mozi_backend.db import dal, database  # noqa: E402
from mozi_backend.vault import embedder, kg  # noqa: E402


def _edges(conn, uid, name, **kw):
    sub = dal.query_kg(conn, uid, name, **kw)
    return {(e["subject"], e["predicate"], e["object"]) for e in sub["edges"]}


def _chain(conn, uid):
    """A-依赖->B, B-使用->C, C-包含->D 链。"""
    dal.ensure_user(conn, uid, f"{uid}@x.cn")
    ids = {n: dal.upsert_entity(conn, user_id=uid, name=n, etype="concept") for n in "ABCD"}
    dal.insert_edge(conn, user_id=uid, subject_id=ids["A"], predicate="依赖",
                    object_id=ids["B"], source_doc_id=None, confidence=0.9)
    dal.insert_edge(conn, user_id=uid, subject_id=ids["B"], predicate="使用",
                    object_id=ids["C"], source_doc_id=None, confidence=0.9)
    dal.insert_edge(conn, user_id=uid, subject_id=ids["C"], predicate="包含",
                    object_id=ids["D"], source_doc_id=None, confidence=0.9)
    return ids


class NormalizeTest(unittest.TestCase):
    def test_normalize_predicate_canon_and_fullwidth(self) -> None:
        self.assertEqual(kg.normalize_predicate("uses"), "使用")
        self.assertEqual(kg.normalize_predicate("利用"), "使用")
        self.assertEqual(kg.normalize_predicate("depends on"), "依赖")
        self.assertEqual(kg.normalize_predicate("使用。"), "使用")        # 去尾标点
        self.assertEqual(kg.normalize_predicate("ｕｓｅｓ"), "使用")       # 全角→半角 NFKC
        self.assertEqual(kg.normalize_predicate("领导"), "领导")          # 未知谓词原样


class UpsertEntityTest(unittest.TestCase):
    def tearDown(self) -> None:
        embedder._reset_backend()

    def test_alias_merge_and_cache_parity(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            id1 = dal.upsert_entity(conn, user_id="u", name="墨子", etype="concept")
            dal._add_alias(conn, id1, "Mozi")
            self.assertEqual(dal.upsert_entity(conn, user_id="u", name="Mozi", etype="concept"), id1)
            self.assertNotEqual(dal.upsert_entity(conn, user_id="u", name="UMA", etype="concept"), id1)
        finally:
            conn.close()

    def test_vector_merge_above_threshold(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            v = [1.0, 0.0, 0.0]
            id1 = dal.upsert_entity(conn, user_id="u", name="北京", etype="city", embedding=v)
            merged = dal.upsert_entity(conn, user_id="u", name="首都北京", etype="city", embedding=v)
            self.assertEqual(merged, id1, "同 type cosine=1≥阈值 → 合并入第一实体")
            # 正交向量不误并
            other = dal.upsert_entity(conn, user_id="u", name="上海", etype="city",
                                      embedding=[0.0, 1.0, 0.0])
            self.assertNotEqual(other, id1)
        finally:
            conn.close()

    def test_mock_vectors_no_false_merge(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            a = dal.upsert_entity(conn, user_id="u", name="墨子", etype="concept",
                                  embedding=embedder.embed("墨子"))
            b = dal.upsert_entity(conn, user_id="u", name="量子计算", etype="concept",
                                  embedding=embedder.embed("量子计算"))
            self.assertNotEqual(a, b, "mock 哈希向量近正交, 不应误并不同实体")
        finally:
            conn.close()

    def test_threshold_from_settings(self) -> None:
        import dataclasses
        conn = fresh_conn()
        orig = dal.settings
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            id1 = dal.upsert_entity(conn, user_id="u", name="X", etype="c", embedding=[1.0, 0.0])
            # 默认 0.92: 近似但不同向量不并
            mid = dal.upsert_entity(conn, user_id="u", name="Y", etype="c", embedding=[0.7, 0.714])
            self.assertNotEqual(mid, id1)
            # 调低阈值 → 合并 (验证读 settings)
            dal.settings = dataclasses.replace(orig, kg_dedup_sim_threshold=0.5)
            id3 = dal.upsert_entity(conn, user_id="u", name="Z", etype="c", embedding=[0.7, 0.714])
            self.assertIn(id3, {id1, mid})
        finally:
            dal.settings = orig
            conn.close()

    def test_user_isolation(self) -> None:
        conn = fresh_conn()
        try:
            for u in ("alice", "bob"):
                dal.ensure_user(conn, u, f"{u}@x.cn")
            a = dal.upsert_entity(conn, user_id="alice", name="墨子", etype="concept")
            b = dal.upsert_entity(conn, user_id="bob", name="墨子", etype="concept")
            self.assertNotEqual(a, b, "跨 user 同名不得合并 (行级隔离)")
        finally:
            conn.close()


class QueryKgTest(unittest.TestCase):
    def test_multihop_monotonic(self) -> None:
        conn = fresh_conn()
        try:
            _chain(conn, "u")
            self.assertEqual(_edges(conn, "u", "A", hops=1), {("A", "依赖", "B")})
            self.assertTrue({("B", "使用", "C")} <= _edges(conn, "u", "A", hops=2))
            self.assertTrue({("C", "包含", "D")} <= _edges(conn, "u", "A", hops=3))
        finally:
            conn.close()

    def test_load_bounded_caps(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            hub = dal.upsert_entity(conn, user_id="u", name="hub", etype="c")
            for i in range(20):
                leaf = dal.upsert_entity(conn, user_id="u", name=f"leaf{i}", etype="c")
                dal.insert_edge(conn, user_id="u", subject_id=hub, predicate="有",
                                object_id=leaf, source_doc_id=None, confidence=0.5)
            sub = dal.query_kg(conn, "u", "hub", hops=1, max_nodes=5, max_edges=5)
            self.assertLessEqual(len(sub["edges"]), 5, "max_edges 截断")
            self.assertLessEqual(len(sub["nodes"]), 6, "max_nodes 截断 (含 root)")
        finally:
            conn.close()

    def test_find_root_exact_over_fuzzy(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            dal.upsert_entity(conn, user_id="u", name="墨", etype="c")
            mozi = dal.upsert_entity(conn, user_id="u", name="墨子", etype="c")
            root = dal._find_root(conn, "u", "墨子")
            self.assertEqual(root["entity_id"], mozi, "精确名优先于模糊")
        finally:
            conn.close()

    def test_isolated_root_returns_self(self) -> None:
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u", "u@x.cn")
            dal.upsert_entity(conn, user_id="u", name="孤岛", etype="c")
            sub = dal.query_kg(conn, "u", "孤岛", hops=2)
            self.assertEqual(len(sub["nodes"]), 1)
            self.assertEqual(sub["edges"], [])
        finally:
            conn.close()

    def test_predicate_histogram_isolation(self) -> None:
        conn = fresh_conn()
        try:
            _chain(conn, "alice")
            _chain(conn, "bob")
            hist = dict(dal.predicate_histogram(conn, "alice"))
            self.assertEqual(hist.get("依赖"), 1)
            self.assertNotIn("__other__", hist)
            self.assertEqual(sum(hist.values()), 3, "只计 alice 的边 (行级隔离)")
        finally:
            conn.close()


class ExtractTriplesTest(unittest.TestCase):
    def test_zero_key_regex_not_real(self) -> None:
        triples, is_real = kg.extract_triples("墨子 依赖 BGE-M3。", active_providers=None)
        self.assertFalse(is_real)
        self.assertIn(("墨子", "依赖", "BGE-M3"), {(t[0], t[1], t[2]) for t in triples})

    def test_llm_parse_real(self) -> None:
        orig = llmmod.complete_sync
        llmmod.complete_sync = lambda *a, **k: (
            '前言 [{"subject":"墨子","predicate":"使用","object":"SQLite","confidence":0.9}] 后语', True)
        try:
            triples, is_real = kg.extract_triples("any", active_providers={"glm"})
            self.assertTrue(is_real)
            self.assertEqual(triples[0][:3], ("墨子", "使用", "SQLite"))
        finally:
            llmmod.complete_sync = orig

    def test_llm_bad_confidence_defaults_not_abort(self) -> None:
        orig = llmmod.complete_sync
        llmmod.complete_sync = lambda *a, **k: (
            '[{"subject":"墨子","predicate":"使用","object":"SQLite","confidence":"high"}]', True)
        try:
            triples, is_real = kg.extract_triples("any", active_providers={"glm"})
            self.assertTrue(is_real)
            self.assertEqual(triples[0][:3], ("墨子", "使用", "SQLite"))
            self.assertEqual(triples[0][3], 0.7, "非数字 confidence → 降级 0.7, 不中断整表抽取")
        finally:
            llmmod.complete_sync = orig

    def test_llm_bad_json_still_real(self) -> None:
        orig = llmmod.complete_sync
        llmmod.complete_sync = lambda *a, **k: ("不是 JSON 的散文", True)
        try:
            triples, is_real = kg.extract_triples("any", active_providers={"glm"})
            self.assertTrue(is_real, "已出网 → is_real=True (供上层补 egress 审计)")
            self.assertEqual(triples, [])
        finally:
            llmmod.complete_sync = orig


class EgressMustLogTest(unittest.TestCase):
    def test_log_egress_now_survives_caller_rollback(self) -> None:
        import uuid
        database.init_db()                 # 确保主库 (settings.db_path) 有表 (独立连接写此库)
        res = f"rolltest-{uuid.uuid4().hex[:8]}"
        with database.get_conn() as caller:   # 主库 (settings.db_path)
            try:
                with database.transaction(caller):
                    dal.log_egress_now("u_roll", "kg.extract.egress", res)  # 独立连接
                    raise RuntimeError("caller rollback")
            except RuntimeError:
                pass
        with database.get_conn() as v:
            n = v.execute("SELECT count(*) FROM audit_log WHERE resource=?", (res,)).fetchone()[0]
        self.assertEqual(n, 1, "出网必留痕: 独立事务不随调用方回滚")


class OrchestratorKgEgressTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, conn, uid, **kw):
        dal.ensure_user(conn, uid, f"{uid}@x.cn")
        sid = dal.create_session(conn, uid, "t", "auto")
        events = []
        async for e in orch.run_chat(conn=conn, user_id=uid, session_id=sid,
                                     user_text="墨子 使用 SQLite。", active_providers=set(),
                                     inject_context=False, **kw):
            events.append(e)
        return events

    async def test_real_extract_logs_split_egress(self) -> None:
        database.init_db()                 # 主库须有表 (log_egress_now 独立连接写 settings.db_path)
        orig = kgmod.extract_triples
        kgmod.extract_triples = lambda *a, **k: ([("墨子", "使用", "SQLite", 0.9)], True)
        conn = fresh_conn()
        try:
            await self._run(conn, "kg_egress_u")
            persist = conn.execute(
                "SELECT egress_flag FROM audit_log WHERE user_id='kg_egress_u' "
                "AND action='kg.extract.persist'").fetchall()
            self.assertEqual(len(persist), 1)
            self.assertEqual(persist[0][0], 0, "persist 行 egress=0 (不双计)")
            # 即落即记的 egress 行落主库 (log_egress_now 独立连接)
            with database.get_conn() as main:
                eg = main.execute(
                    "SELECT egress_flag FROM audit_log WHERE user_id='kg_egress_u' "
                    "AND action='kg.extract.egress'").fetchall()
            self.assertTrue(eg and eg[0][0] == 1, "出网即记 kg.extract.egress egress=1")
        finally:
            kgmod.extract_triples = orig
            conn.close()

    async def test_mock_extract_no_egress(self) -> None:
        conn = fresh_conn()
        try:
            await self._run(conn, "kg_mock_u")   # 零 key → 正则, is_real False
            rows = conn.execute(
                "SELECT count(*) FROM audit_log WHERE user_id='kg_mock_u' "
                "AND action LIKE 'kg.extract%'").fetchone()[0]
            self.assertEqual(rows, 0, "零 key 不产生 kg.extract 审计")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
