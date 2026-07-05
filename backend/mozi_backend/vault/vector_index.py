"""sqlite-vec 暴力 KNN 索引 (vec0 虚表)。缺扩展全部降级 no-op, 调用方走 bruteforce。

每入口先幂等 _load_vec(当前裸连接) —— database._connect 每请求新建且不 load 扩展。
user_id + embed_model 双过滤 (行级隔离 + 同模型同维, 混维不串)。
"""
from __future__ import annotations

from ..config import settings
from ..util import jload

_LOADED_CONNS: set[int] = set()   # 按 id(conn) 缓存, 防重复 load
_FAILED_CONNS: set[int] = set()   # 探测失败的 conn, 不重试


def _load_vec(conn) -> bool:
    cid = id(conn)
    if cid in _LOADED_CONNS:
        return True
    if cid in _FAILED_CONNS:
        return False
    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.execute("SELECT vec_version()")
        _LOADED_CONNS.add(cid)
        return True
    except Exception:
        _FAILED_CONNS.add(cid)
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass


def _forget_conn(conn) -> None:
    """conn 关闭前清理缓存 (防 id 复用误判已 load)。由 database.get_conn finally 调用。"""
    cid = id(conn)
    _LOADED_CONNS.discard(cid)
    _FAILED_CONNS.discard(cid)


def backend(conn) -> str:
    if settings.ann_backend == "bruteforce":
        return "bruteforce"
    return "vec" if _load_vec(conn) else "bruteforce"


def ensure_index(conn, dim: int) -> None:
    if not _load_vec(conn):
        return
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
        f"chunk_id TEXT PRIMARY KEY, user_id TEXT, embed_model TEXT, embedding float[{int(dim)}])")


def upsert(conn, chunk_id: str, user_id: str, model: str, vec: list[float]) -> None:
    if not _load_vec(conn):
        return
    import sqlite_vec
    try:
        conn.execute("INSERT OR REPLACE INTO vec_chunks(chunk_id,user_id,embed_model,embedding) "
                     "VALUES(?,?,?,?)", (chunk_id, user_id, model, sqlite_vec.serialize_float32(vec)))
    except Exception:
        _FAILED_CONNS.add(id(conn))   # 维度不符/表缺 → 降级, 调用方走 bruteforce


def query(conn, user_id: str, qvec: list[float], model: str, k: int) -> list[tuple[str, float]]:
    """ANN top-k。user_id+embed_model 双过滤。L2 距离 → cosine 相似 (归一向量: cos=1-L2^2/2)。"""
    if not _load_vec(conn):
        return []
    import sqlite_vec
    try:
        rows = conn.execute(
            "SELECT chunk_id, distance FROM vec_chunks "
            "WHERE user_id=? AND embed_model=? AND embedding MATCH ? ORDER BY distance LIMIT ?",
            (user_id, model, sqlite_vec.serialize_float32(qvec), k)).fetchall()
    except Exception:
        return []
    return [(r["chunk_id"], 1.0 - (r["distance"] ** 2) / 2.0) for r in rows]


def rebuild(conn, user_id: str) -> None:
    """从 embeddings(权威源) 全量重建该 user 当前 active_model 的 vec_chunks (维度切换后)。"""
    if not _load_vec(conn):
        return
    from . import embedder
    model = embedder.active_model()
    ensure_index(conn, embedder.active_dim())
    conn.execute("DELETE FROM vec_chunks WHERE user_id=?", (user_id,))
    rows = conn.execute(
        """SELECT c.chunk_id, e.vector, e.embed_model
           FROM doc_chunks c
           JOIN vault_documents d ON d.doc_id=c.doc_id
           JOIN embeddings e ON e.chunk_id=c.chunk_id
           WHERE d.user_id=? AND e.embed_model=?""", (user_id, model)).fetchall()
    for r in rows:
        vec = jload(r["vector"], [])
        if vec:
            upsert(conn, r["chunk_id"], user_id, model, vec)


__all__ = ["backend", "ensure_index", "upsert", "query", "rebuild", "_load_vec", "_forget_conn"]
