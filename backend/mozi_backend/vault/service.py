"""Vault 归档闭环: 文档 → 分块 → 向量化 → KG 抽取/回填 (§8.4 图3核心工作流)。"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..db import dal
from . import chunking, embedder, kg, vector_index


def _load_alias_index(conn: sqlite3.Connection, user_id: str) -> dict[str, str]:
    """批内一次性 load 该 user 实体 name/alias → entity_id 映射 (归档热路径消全表扫)。"""
    from ..util import jload
    idx: dict[str, str] = {}
    for r in conn.execute("SELECT entity_id,name,aliases FROM kg_entities WHERE user_id=?", (user_id,)):
        idx[r["name"]] = r["entity_id"]
        for a in jload(r["aliases"], []):
            idx.setdefault(a, r["entity_id"])
    return idx


def archive_document(conn: sqlite3.Connection, *, user_id: str, title: str, content: str,
                     doc_type: str = "笔记", storage_mode: str = "local",
                     kg_source: str | None = None, triples=None,
                     privacy_tier: str = "local_first") -> dict[str, Any]:
    """归档一篇文档并建立全部索引。返回统计。

    kg_source: KG 三元组抽取源。None → 用 content; 传入 → 只从该文本抽 (对话只抽 user 提问)。
    triples: 调用方 (orchestrator) 已在事务外抽好的三元组。None → 本函数自抽 (用于 /v1/vault/archive
             纯正则零外呼路径, active_providers=None)。privacy_tier 仅自抽时透传, 严禁硬编码。
    """
    doc_id = dal.create_document(conn, user_id=user_id, doc_type=doc_type, title=title, storage_mode=storage_mode)

    # 分块 + 向量化
    chunks = chunking.chunk_text(content)
    chunk_ids: list[str] = []
    model = embedder.active_model()
    dim = embedder.active_dim()
    for ordinal, ctext in enumerate(chunks):
        vec = embedder.embed(ctext)
        cid = dal.insert_chunk(
            conn, doc_id=doc_id, ordinal=ordinal, text=ctext,
            token_count=chunking.count_tokens(ctext), vector=vec, embed_model=model, dim=dim,
        )
        vector_index.upsert(conn, cid, user_id, model, vec)   # 无 sqlite-vec → no-op
        dal.fts_upsert(conn, cid, user_id, ctext)             # 无 FTS5 → 静默跳过
        chunk_ids.append(cid)

    # KG 回填: triples 由调用方事务外抽好则直接用; None → 本地自抽 (纯正则零外呼)
    if triples is None:
        triples, _is_real = kg.extract_triples(
            kg_source if kg_source is not None else content,
            active_providers=None, privacy_tier=privacy_tier)
    alias_index = _load_alias_index(conn, user_id)   # 批内缓存, 消全表扫
    edge_count = 0
    for t in triples:
        subj, pred, obj, conf = t[0], t[1], t[2], t[3]
        s_type = t[4] if len(t) > 4 else "concept"
        o_type = t[5] if len(t) > 5 else "concept"
        s_id = dal.upsert_entity(conn, user_id=user_id, name=subj, etype=s_type,
                                 embedding=embedder.embed(subj), _alias_index=alias_index)
        o_id = dal.upsert_entity(conn, user_id=user_id, name=obj, etype=o_type,
                                 embedding=embedder.embed(obj), _alias_index=alias_index)
        dal.insert_edge(conn, user_id=user_id, subject_id=s_id,
                        predicate=kg.normalize_predicate(pred), object_id=o_id,
                        source_doc_id=doc_id, confidence=conf)
        edge_count += 1

    # 本地归档不出网
    dal.log_audit(conn, user_id=user_id, action="vault.archive", resource=doc_id, egress=False)
    return {
        "doc_id": doc_id,
        "title": title,
        "chunks": len(chunk_ids),
        "triples": edge_count,
        "storage_mode": storage_mode,
    }
