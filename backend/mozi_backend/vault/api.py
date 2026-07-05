"""Vault / Mozi-KG API (§7.2): archive / search / kg_query。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..auth import current_user_id
from ..config import settings
from ..db import dal
from ..db.database import get_conn
from ..schemas import ArchiveRequest, KGQueryRequest, VaultSearchRequest
from . import retrieval, service

router = APIRouter(prefix="/v1", tags=["vault"])


@router.post("/vault/archive")
def vault_archive(body: ArchiveRequest, user_id: str = Depends(current_user_id)):
    with get_conn() as conn:
        dal.ensure_user(conn, user_id, f"{user_id}@mozi.local", settings.default_region)
        return service.archive_document(conn, user_id=user_id, title=body.title,
                                        content=body.content, doc_type=body.type,
                                        storage_mode=body.storage_mode)


@router.post("/vault/search")
def vault_search(body: VaultSearchRequest, user_id: str = Depends(current_user_id)):
    with get_conn() as conn:
        res = retrieval.search(conn, user_id, body.query, k=body.k, routes=body.routes)
        dal.log_retrieval(conn, user_id=user_id, query=body.query,
                          routes=[h.chunk_id for h in res.hits], latency_ms=res.latency_ms,
                          injected=res.injected)
    return res.to_dict()


@router.get("/vault/documents")
def vault_documents(user_id: str = Depends(current_user_id)):
    with get_conn() as conn:
        rows = dal.list_documents(conn, user_id)
    return {"documents": [dict(r) for r in rows]}


@router.get("/vault/documents/{doc_id}")
def vault_document(doc_id: str, user_id: str = Depends(current_user_id)):
    with get_conn() as conn:
        doc = dal.get_document(conn, user_id, doc_id)
        if not doc:
            return JSONResponse({"error": "not found"}, status_code=404)
        chunks = conn.execute(
            "SELECT chunk_id,ordinal,text,token_count FROM doc_chunks WHERE doc_id=? ORDER BY ordinal",
            (doc_id,),
        ).fetchall()
    return {"document": dict(doc), "chunks": [dict(c) for c in chunks]}


@router.post("/kg/query")
def kg_query(body: KGQueryRequest, user_id: str = Depends(current_user_id)):
    with get_conn() as conn:
        return dal.query_kg(conn, user_id, body.entity, hops=body.hops,
                            max_nodes=body.max_nodes, max_edges=body.max_edges)


@router.get("/kg/graph")
def kg_graph(user_id: str = Depends(current_user_id), limit: int = 100):
    """全图 (可视化用)。"""
    with get_conn() as conn:
        nodes = conn.execute(
            "SELECT entity_id,name,type FROM kg_entities WHERE user_id=? LIMIT ?", (user_id, limit)
        ).fetchall()
        edges = conn.execute(
            """SELECT s.name AS subject, e.predicate, o.name AS object, e.confidence
               FROM kg_edges e JOIN kg_entities s ON s.entity_id=e.subject_id
               JOIN kg_entities o ON o.entity_id=e.object_id
               WHERE e.user_id=? LIMIT ?""", (user_id, limit),
        ).fetchall()
    return {"nodes": [dict(n) for n in nodes], "edges": [dict(e) for e in edges]}
