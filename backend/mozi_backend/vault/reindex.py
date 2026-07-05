"""切换 embedding 后端 (mock↔真实 BGE-M3, 256↔1024) 后重嵌入 + 重建索引。

零外呼: 仅本地 embedder; 行级隔离: 严格按 user_id; 事务: 整 user 原子, 失败回滚。
用法:
  .venv/bin/python -m mozi_backend.vault.reindex --user u_demo
  .venv/bin/python -m mozi_backend.vault.reindex --all
  .venv/bin/python -m mozi_backend.vault.reindex --user u_demo --dry-run
"""
from __future__ import annotations

import argparse

from ..db import dal, database
from ..util import jdump
from . import embedder, vector_index


def reindex_user(conn, user_id: str, *, dry_run: bool = False) -> dict:
    rows = conn.execute(
        "SELECT c.chunk_id, c.text, e.embed_model AS old_model, e.dim AS old_dim "
        "FROM doc_chunks c JOIN vault_documents d ON d.doc_id=c.doc_id "
        "LEFT JOIN embeddings e ON e.chunk_id=c.chunk_id WHERE d.user_id=?",
        (user_id,)).fetchall()
    new_model = embedder.active_model()
    new_dim = embedder.active_dim()
    stats = {"user_id": user_id, "chunks": len(rows), "new_model": new_model, "new_dim": new_dim,
             "old_models": sorted({r["old_model"] for r in rows if r["old_model"]})}
    if dry_run or not rows:
        return stats
    with database.transaction(conn):                 # 整 user 重嵌入原子
        vector_index.ensure_index(conn, new_dim)     # 无扩展 no-op
        for r in rows:
            vec = embedder.embed(r["text"])
            conn.execute("UPDATE embeddings SET vector=?, embed_model=?, dim=? WHERE chunk_id=?",
                         (jdump(vec), new_model, new_dim, r["chunk_id"]))
            dal.fts_upsert(conn, r["chunk_id"], user_id, r["text"])   # 重写倒排同口径
        vector_index.rebuild(conn, user_id)          # 旧维度行清掉, 按 new_dim 重建
    dal.log_audit(conn, user_id=user_id, action="vault.reindex", resource=new_model, egress=False)
    return stats


def reindex_all(conn) -> list[dict]:
    uids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    return [reindex_user(conn, u) for u in uids]


def main() -> None:
    ap = argparse.ArgumentParser(description="墨子检索 reindex (切后端后重嵌入)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--user")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    with database.get_conn() as conn:                # finally 自动 _forget_conn + close
        out = reindex_all(conn) if a.all else [reindex_user(conn, a.user, dry_run=a.dry_run)]
    for s in out:
        print(f"[reindex] user={s['user_id']} chunks={s['chunks']} "
              f"{s.get('old_models')}->{s['new_model']} dim->{s['new_dim']}"
              + (" (dry-run)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
