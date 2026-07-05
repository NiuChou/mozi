"""多路检索 + RRF 融合 + Self-RAG-lite (§8.3 推理/检索层)。

路: dense (BGE-M3 cosine) + sparse (BM25)。RRF 倒数秩融合, 重排兜底。
Self-RAG-lite: 用融合分判相关性, 低于地板则不注入 (自检纠错的轻量替身)。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..db import dal
from ..util import jload
from . import embedder, vector_index
from .bm25 import BM25

logger = logging.getLogger("mozi.retrieval")
RRF_K = 60          # RRF 常数 (有理论含义, 保留)
RANK_REF = 8        # 自适应注入门参考深度: 比单路第 8 位命中更弱视为长尾噪声
_DIM_WARNED: set[str] = set()   # 模块级节流: 每进程每 user 混维只告警一次


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    title: str
    text: str
    score: float
    routes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "title": self.title,
            "text": self.text,
            "score": round(self.score, 5),
            "provenance": f"{self.title} · {self.chunk_id}",
            "routes": self.routes,
        }


@dataclass
class RetrievalResult:
    hits: list[Hit]
    latency_ms: int
    injected: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [h.to_dict() for h in self.hits],
            "latency_ms": self.latency_ms,
            "injected": self.injected,
        }


def _rrf(ranklists: dict[str, list[str]]) -> dict[str, float]:
    """RRF: score(d) = Σ 1/(k + rank)。ranklists: {route: [chunk_id 按相关降序]}"""
    fused: dict[str, float] = {}
    for ids in ranklists.values():
        for rank, cid in enumerate(ids):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return fused


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _inject_gate(hits: list[Hit], ranklists: dict[str, list[str]]) -> bool:
    """Self-RAG-lite 注入门。inject_floor 显式覆盖固定地板; 否则自适应 (锚单路第 RANK_REF 位)。"""
    if not hits:
        return False
    if settings.inject_floor is not None:
        return hits[0].score >= settings.inject_floor
    n_routes = max(len(ranklists), 1)
    base = 1.0 / (RRF_K + 1 + RANK_REF)              # ≈ 1/69 ≈ 0.0145
    floor = base * (1 + 0.5 * (n_routes - 1))        # 命中路数越多门越高
    return hits[0].score >= floor


def _warn_if_mixed(conn: sqlite3.Connection, user_id: str, rows) -> None:
    """混维静默少召回 → 显式可观测: 旧 embed_model 不参与检索时审计 + 日志提示 reindex。"""
    active = embedder.active_model()
    stale = {r["embed_model"] for r in rows if r["embed_model"] and r["embed_model"] != active}
    if stale and user_id not in _DIM_WARNED:
        _DIM_WARNED.add(user_id)
        dal.log_audit(conn, user_id=user_id, action="retrieval.dim_mismatch",
                      resource=f"{sorted(stale)}->{active}", egress=False)
        logger.warning("[retrieval] user=%s 存在旧 embed_model %s 不参与检索, 跑 reindex: "
                       "python -m mozi_backend.vault.reindex --user %s", user_id, sorted(stale), user_id)


def search(conn: sqlite3.Connection, user_id: str, query: str, k: int = 5,
           routes: list[str] | None = None) -> RetrievalResult:
    routes = routes or ["bm25", "dense"]
    t0 = time.perf_counter()
    rows = dal.chunks_with_vectors(conn, user_id)
    if not rows:
        return RetrievalResult([], int((time.perf_counter() - t0) * 1000), False)

    _warn_if_mixed(conn, user_id, rows)
    meta = {r["chunk_id"]: r for r in rows}
    ranklists: dict[str, list[str]] = {}
    active_model = embedder.active_model()
    width = max(k * 4, 20)

    if "bm25" in routes:
        if settings.bm25_backend != "memory" and _table_exists(conn, "chunks_fts"):
            ranklists["bm25"] = [cid for cid, _ in dal.fts_search(conn, user_id, query, top_k=width)]
        else:                                        # 兜底: 内存 BM25 (FTS5 不可用)
            bm = BM25()
            bm.index([(r["chunk_id"], r["text"]) for r in rows])
            ranklists["bm25"] = [cid for cid, _ in bm.search(query, top_k=width)]

    if "dense" in routes:
        qv = embedder.embed(query)
        if vector_index.backend(conn) == "vec":
            scored = vector_index.query(conn, user_id, qv, active_model, k=width)
        else:                                        # 兜底: 暴力 cosine, 仅比对同 model 向量 (混维不串)
            scored = []
            for r in rows:
                if r["embed_model"] and r["embed_model"] != active_model:
                    continue
                vec = jload(r["vector"], [])
                if vec and len(vec) == len(qv):
                    scored.append((r["chunk_id"], embedder.cosine(qv, vec)))
            scored.sort(key=lambda x: x[1], reverse=True)
            scored = scored[:width]
        ranklists["dense"] = [cid for cid, s in scored if s > 0]

    fused = _rrf(ranklists)
    ordered = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:k]

    hits: list[Hit] = []
    for cid, score in ordered:
        r = meta[cid]
        present = [route for route, ids in ranklists.items() if cid in ids]
        hits.append(Hit(cid, r["doc_id"], r["title"] or "未命名", r["text"], score, present))

    latency_ms = int((time.perf_counter() - t0) * 1000)
    return RetrievalResult(hits, latency_ms, _inject_gate(hits, ranklists))
