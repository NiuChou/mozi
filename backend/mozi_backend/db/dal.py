"""数据访问层 (DAL)。所有写按 user_id 行级隔离 (§9)。函数显式接 conn 以控事务。"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..config import settings
from ..util import jdump, jload, new_id, now, period_now

Row = sqlite3.Row

# ---------- users ----------

def ensure_user(conn: sqlite3.Connection, user_id: str, email: str, region: str = "CN") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO users(user_id,email,region,created_at) VALUES(?,?,?,?)",
        (user_id, email, region, now()),
    )


def get_user(conn: sqlite3.Connection, user_id: str) -> Row | None:
    return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


# ---------- sessions / messages ----------

def create_session(conn: sqlite3.Connection, user_id: str, title: str, model_policy: str) -> str:
    sid = new_id("sess")
    conn.execute(
        "INSERT INTO sessions(session_id,user_id,title,model_policy,created_at) VALUES(?,?,?,?,?)",
        (sid, user_id, title, model_policy, now()),
    )
    return sid


def list_sessions(conn: sqlite3.Connection, user_id: str, *, archived: bool = False) -> list[Row]:
    """默认仅返回未归档会话; archived=True 仅返回已归档 (归档视图)。"""
    return conn.execute(
        "SELECT * FROM sessions WHERE user_id=? AND archived=? ORDER BY created_at DESC",
        (user_id, 1 if archived else 0),
    ).fetchall()


def get_session(conn: sqlite3.Connection, user_id: str, session_id: str) -> Row | None:
    return conn.execute(
        "SELECT * FROM sessions WHERE session_id=? AND user_id=?", (session_id, user_id)
    ).fetchone()


def rename_session(conn: sqlite3.Connection, user_id: str, session_id: str, title: str) -> bool:
    """改标题 (限本人会话)。返回是否命中。"""
    cur = conn.execute(
        "UPDATE sessions SET title=? WHERE session_id=? AND user_id=?",
        (title, session_id, user_id),
    )
    return cur.rowcount > 0


def set_session_archived(conn: sqlite3.Connection, user_id: str, session_id: str, archived: bool) -> bool:
    """软归档/恢复 (限本人会话)。返回是否命中。"""
    cur = conn.execute(
        "UPDATE sessions SET archived=? WHERE session_id=? AND user_id=?",
        (1 if archived else 0, session_id, user_id),
    )
    return cur.rowcount > 0


def delete_session(conn: sqlite3.Connection, user_id: str, session_id: str) -> bool:
    """硬删会话 (限本人)。messages 经 FK ON DELETE CASCADE 连带清除。返回是否命中。"""
    cur = conn.execute(
        "DELETE FROM sessions WHERE session_id=? AND user_id=?", (session_id, user_id)
    )
    return cur.rowcount > 0


def add_message(conn: sqlite3.Connection, session_id: str, role: str, content: str,
                model: str | None = None, *, routing_meta=None, hits=None,
                injected: bool = False, usage_meta=None) -> str:
    """per-message 元数据 keyword-only 可选 (旧位置参调用零改动)。"""
    mid = new_id("msg")
    conn.execute(
        "INSERT INTO messages(message_id,session_id,role,content_ref,model,"
        "routing_meta,hits,usage_meta,injected,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (mid, session_id, role, content, model,
         jdump(routing_meta) if routing_meta is not None else None,
         jdump(hits) if hits is not None else None,
         jdump(usage_meta) if usage_meta is not None else None,
         int(injected), now()),
    )
    return mid


def list_messages(conn: sqlite3.Connection, session_id: str) -> list[Row]:
    # created_at 秒级粒度, 同轮 user+assistant 常同秒 → 补 rowid 次级键保证确定序
    # (续传尾态判定与 openSession 回填不再因平手行序不定误判)。
    return conn.execute(
        "SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC, rowid ASC", (session_id,)
    ).fetchall()


# ---------- model_calls / usage ----------

def record_model_call(conn: sqlite3.Connection, *, user_id: str, message_id: str | None, provider: str,
                      model: str, tokens_in: int, tokens_out: int, cost_cny: float,
                      latency_ms: int, strategy: str, fallback_used: bool) -> str:
    cid = new_id("call")
    conn.execute(
        """INSERT INTO model_calls(call_id,user_id,message_id,provider,model,tokens_in,tokens_out,
           cost_cny,latency_ms,strategy,fallback_used,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, user_id, message_id, provider, model, tokens_in, tokens_out, cost_cny,
         latency_ms, strategy, int(fallback_used), now()),
    )
    bump_usage(conn, user_id, tokens_in + tokens_out, cost_cny)
    return cid


def bump_usage(conn: sqlite3.Connection, user_id: str, tokens: int, cost_cny: float) -> None:
    period = period_now()
    row = conn.execute(
        "SELECT entry_id,tokens_used,requests,cost_cny FROM usage_ledger WHERE user_id=? AND period=?",
        (user_id, period),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE usage_ledger SET tokens_used=?,requests=?,cost_cny=? WHERE entry_id=?",
            (row["tokens_used"] + tokens, row["requests"] + 1, round(row["cost_cny"] + cost_cny, 6), row["entry_id"]),
        )
    else:
        conn.execute(
            "INSERT INTO usage_ledger(entry_id,user_id,period,tokens_used,requests,cost_cny) VALUES(?,?,?,?,?,?)",
            (new_id("usg"), user_id, period, tokens, 1, round(cost_cny, 6)),
        )


def usage_summary(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT tokens_used,requests,cost_cny FROM usage_ledger WHERE user_id=? AND period=?",
        (user_id, period_now()),
    ).fetchone()
    if not row:
        return {"period": period_now(), "tokens_used": 0, "requests": 0, "cost_cny": 0.0}
    return {"period": period_now(), **dict(row)}


# ---------- vault documents / chunks / embeddings ----------

def create_document(conn: sqlite3.Connection, *, user_id: str, doc_type: str, title: str,
                    storage_mode: str = "local") -> str:
    did = new_id("doc")
    ts = now()
    conn.execute(
        """INSERT INTO vault_documents(doc_id,user_id,type,title,crdt_state,storage_mode,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (did, user_id, doc_type, title, None, storage_mode, ts, ts),
    )
    return did


def list_documents(conn: sqlite3.Connection, user_id: str) -> list[Row]:
    return conn.execute(
        """SELECT d.*, (SELECT COUNT(*) FROM doc_chunks c WHERE c.doc_id=d.doc_id) AS chunk_count
           FROM vault_documents d WHERE d.user_id=? ORDER BY d.updated_at DESC""",
        (user_id,),
    ).fetchall()


def get_document(conn: sqlite3.Connection, user_id: str, doc_id: str) -> Row | None:
    return conn.execute(
        "SELECT * FROM vault_documents WHERE doc_id=? AND user_id=?", (doc_id, user_id)
    ).fetchone()


def insert_chunk(conn: sqlite3.Connection, *, doc_id: str, ordinal: int, text: str,
                 token_count: int, vector: list[float], embed_model: str,
                 dim: int | None = None) -> str:
    chunk_id = new_id("chk")
    conn.execute(
        "INSERT INTO doc_chunks(chunk_id,doc_id,ordinal,text,token_count) VALUES(?,?,?,?,?)",
        (chunk_id, doc_id, ordinal, text, token_count),
    )
    conn.execute(
        "INSERT INTO embeddings(chunk_id,vector,embed_model,dim) VALUES(?,?,?,?)",
        (chunk_id, jdump(vector), embed_model, dim if dim is not None else len(vector)),
    )
    return chunk_id


def fts_upsert(conn: sqlite3.Connection, chunk_id: str, user_id: str, text: str) -> None:
    """写 FTS5 倒排 (chunks_fts 可用时)。unicode61 档预切词 (与 BM25 同口径); 表缺则静默跳过。"""
    from ..vault import embedder
    seg = " ".join(embedder.tokenize(text))
    try:
        # 幂等: FTS5 无唯一约束, 裸 INSERT 会在 reindex/重归档时追加重复行 → 污染 BM25 IDF。先删旧行再插。
        conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk_id,))
        conn.execute("INSERT INTO chunks_fts(chunk_id,user_id,text) VALUES(?,?,?)",
                     (chunk_id, user_id, seg))
    except sqlite3.OperationalError:
        pass


def fts_search(conn: sqlite3.Connection, user_id: str, query: str, top_k: int) -> list[tuple[str, float]]:
    """FTS5 bm25() 排序 (越小越相关 → 取负升序)。OR 连接 token 防 AND 零召回。"""
    from ..vault import embedder
    toks = [t for t in embedder.tokenize(query) if t]
    if not toks:
        return []
    match = " OR ".join(toks)
    rows = conn.execute(
        "SELECT chunk_id, bm25(chunks_fts) AS s FROM chunks_fts "
        "WHERE user_id=? AND chunks_fts MATCH ? ORDER BY s LIMIT ?",
        (user_id, match, top_k)).fetchall()
    return [(r["chunk_id"], -r["s"]) for r in rows]


def chunks_with_vectors(conn: sqlite3.Connection, user_id: str) -> list[Row]:
    """该用户全部 chunk + 向量 + 出处 (检索用)。"""
    return conn.execute(
        """SELECT c.chunk_id, c.doc_id, c.ordinal, c.text, e.vector, e.embed_model, e.dim, d.title
           FROM doc_chunks c
           JOIN vault_documents d ON d.doc_id=c.doc_id
           LEFT JOIN embeddings e ON e.chunk_id=c.chunk_id
           WHERE d.user_id=?""",
        (user_id,),
    ).fetchall()


# ---------- KG ----------

def _add_alias(conn: sqlite3.Connection, entity_id: str, alias: str) -> None:
    row = conn.execute("SELECT aliases FROM kg_entities WHERE entity_id=?", (entity_id,)).fetchone()
    al = jload(row["aliases"], []) if row else []
    if alias not in al:
        al.append(alias)
        conn.execute("UPDATE kg_entities SET aliases=? WHERE entity_id=?", (jdump(al), entity_id))


def upsert_entity(conn: sqlite3.Connection, *, user_id: str, name: str, etype: str,
                  embedding: list[float] | None = None,
                  sim_threshold: float | None = None, _alias_index: dict | None = None) -> str:
    """三级消歧: 精确名 → alias 命中 → 同 type 向量 cosine≥阈值 (并入 aliases) → 新建。

    sim_threshold 默认读 settings (真实 BGE-M3 标定); 向量扫描限 scan_cap 防爆;
    _alias_index 批内缓存 (归档热路径消全表扫)。全程带 user_id 行级隔离。
    """
    if sim_threshold is None:
        sim_threshold = settings.kg_dedup_sim_threshold
    if _alias_index is not None and name in _alias_index:
        return _alias_index[name]
    row = conn.execute("SELECT entity_id FROM kg_entities WHERE user_id=? AND name=?",
                       (user_id, name)).fetchone()
    if row:
        if _alias_index is not None:
            _alias_index[name] = row["entity_id"]
        return row["entity_id"]
    for r in conn.execute("SELECT entity_id,aliases FROM kg_entities WHERE user_id=?", (user_id,)):
        if name in jload(r["aliases"], []):
            if _alias_index is not None:
                _alias_index[name] = r["entity_id"]
            return r["entity_id"]
    if embedding:
        from ..vault import embedder
        best_id, best_sim = None, 0.0
        for r in conn.execute(
            "SELECT entity_id,embedding FROM kg_entities "
            "WHERE user_id=? AND type=? AND embedding IS NOT NULL ORDER BY rowid DESC LIMIT ?",
            (user_id, etype, settings.kg_dedup_scan_cap)):
            ev = jload(r["embedding"], [])
            sim = embedder.cosine(embedding, ev) if ev else 0.0
            if sim > best_sim:
                best_id, best_sim = r["entity_id"], sim
        if best_id and best_sim >= sim_threshold:
            _add_alias(conn, best_id, name)
            if _alias_index is not None:
                _alias_index[name] = best_id
            return best_id
    eid = new_id("ent")
    conn.execute(
        "INSERT INTO kg_entities(entity_id,user_id,type,name,aliases,embedding) VALUES(?,?,?,?,?,?)",
        (eid, user_id, etype, name, jdump([]), jdump(embedding) if embedding else None),
    )
    if _alias_index is not None:
        _alias_index[name] = eid
    return eid


def predicate_histogram(conn: sqlite3.Connection, user_id: str) -> list[tuple[str, int]]:
    """该 user 谓词分布 (降序), 供运维决定哪些长尾谓词加进 _PRED_CANON。行级隔离, 不出网。"""
    return [(r["predicate"], r["count"]) for r in conn.execute(
        "SELECT predicate, COUNT(*) AS count FROM kg_edges WHERE user_id=? "
        "GROUP BY predicate ORDER BY count DESC", (user_id,)).fetchall()]


def log_egress_now(user_id: str, action: str, resource: str = "pending", egress: bool = True) -> None:
    """出网物理事实即落即记 (独立连接独立短事务, 不随调用方业务事务回滚)。

    METER egress.py 已上线, 但本函数语义为'出网必留痕'(先于业务事务、不可回滚), 故独立连接。
    """
    from .database import get_conn, transaction
    with get_conn() as c:
        with transaction(c):
            c.execute(
                "INSERT INTO audit_log(audit_id,user_id,action,resource,egress_flag,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (new_id("aud"), user_id, action, resource, int(egress), now()))


def insert_edge(conn: sqlite3.Connection, *, user_id: str, subject_id: str, predicate: str,
                object_id: str, source_doc_id: str | None, confidence: float) -> str:
    edge_id = new_id("edge")
    conn.execute(
        """INSERT INTO kg_edges(edge_id,user_id,subject_id,predicate,object_id,valid_from,valid_to,
           asserted_at,source_doc_id,confidence) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (edge_id, user_id, subject_id, predicate, object_id, now(), None, now(), source_doc_id, confidence),
    )
    return edge_id


def _find_root(conn: sqlite3.Connection, user_id: str, entity_name: str):
    """根实体匹配: 精确名 → alias 精确 → 模糊 LIKE 最短名优先。"""
    row = conn.execute("SELECT entity_id,name,type FROM kg_entities WHERE user_id=? AND name=?",
                       (user_id, entity_name)).fetchone()
    if row:
        return row
    for r in conn.execute("SELECT entity_id,name,type,aliases FROM kg_entities WHERE user_id=?",
                          (user_id,)):
        if entity_name in jload(r["aliases"], []):
            return r
    return conn.execute(
        "SELECT entity_id,name,type FROM kg_entities WHERE user_id=? AND name LIKE ? "
        "ORDER BY (name=?) DESC, length(name) ASC LIMIT 1",
        (user_id, f"%{entity_name}%", entity_name)).fetchone()


def _expand_subgraph(conn: sqlite3.Connection, user_id: str, root_id: str,
                     hops: int, max_nodes: int, max_edges: int):
    """从 root 逐 hop 用索引拉相邻边, 全程钳 node/edge 预算 —— load 阶段截断 (与总边数解耦)。

    走 idx_kg_edges_subject/object, 每层只查上一层新增节点的邻边; 任一预算耗尽即停。
    """
    visited = {root_id}
    frontier = {root_id}
    collected: list = []
    seen_edge: set = set()
    for _ in range(hops):
        if not frontier or len(visited) >= max_nodes or len(collected) >= max_edges:
            break
        fmarks = ",".join("?" * len(frontier))
        rows = conn.execute(
            f"""SELECT e.edge_id,e.predicate,e.confidence,e.subject_id,e.object_id,
                       s.name AS subject, o.name AS object
                FROM kg_edges e
                JOIN kg_entities s ON s.entity_id=e.subject_id
                JOIN kg_entities o ON o.entity_id=e.object_id
                WHERE e.user_id=? AND (e.subject_id IN ({fmarks}) OR e.object_id IN ({fmarks}))
                LIMIT ?""",
            (user_id, *frontier, *frontier, max_edges * 2)).fetchall()   # 单层 SQL 自带上限防星型爆
        nxt = set()
        for e in rows:
            if e["edge_id"] in seen_edge:
                continue
            if len(collected) >= max_edges:
                break
            seen_edge.add(e["edge_id"])
            collected.append(e)
            for nid in (e["subject_id"], e["object_id"]):
                if nid not in visited and len(visited) < max_nodes:
                    visited.add(nid)
                    nxt.add(nid)
        frontier = nxt
    return collected, visited


def query_kg(conn: sqlite3.Connection, user_id: str, entity_name: str, hops: int = 1,
             max_nodes: int = 200, max_edges: int = 400) -> dict[str, Any]:
    """以 entity 为中心取 N-hop 子图 (DB 侧分层有界 BFS, load 阶段截断)。hops 钳 [1,5]。"""
    root = _find_root(conn, user_id, entity_name)
    if not root:
        return {"nodes": [], "edges": []}
    hops = max(1, min(hops, 5))
    collected, node_ids = _expand_subgraph(conn, user_id, root["entity_id"], hops, max_nodes, max_edges)
    if not collected:                                    # root 孤立
        return {"nodes": [dict(root)], "edges": []}
    qmarks = ",".join("?" * len(node_ids))
    nodes = conn.execute(
        f"SELECT entity_id,name,type FROM kg_entities WHERE entity_id IN ({qmarks})",
        tuple(node_ids)).fetchall()
    return {
        "nodes": [dict(n) for n in nodes],
        "edges": [{"subject": e["subject"], "predicate": e["predicate"],
                   "object": e["object"], "confidence": e["confidence"]} for e in collected],
    }


# ---------- logs / audit ----------

def log_retrieval(conn: sqlite3.Connection, *, user_id: str, query: str, routes: Any,
                  latency_ms: int, injected: bool) -> None:
    conn.execute(
        "INSERT INTO retrieval_logs(log_id,user_id,query,routes,latency_ms,injected,created_at) VALUES(?,?,?,?,?,?,?)",
        (new_id("rlog"), user_id, query, jdump(routes), latency_ms, int(injected), now()),
    )


def log_audit(conn: sqlite3.Connection, *, user_id: str, action: str, resource: str, egress: bool,
              via_guard: bool = False) -> None:
    """唯一审计写入。egress=True 但未经 egress.audit 门 (via_guard=False) → 软告警 (架构红线)。"""
    if egress and not via_guard:
        from ..telemetry import events  # 惰性 import 避免循环依赖
        events.capture("egress_bypass_warn", {"action": action, "resource": resource[:80]}, user_id)
    conn.execute(
        "INSERT INTO audit_log(audit_id,user_id,action,resource,egress_flag,created_at) VALUES(?,?,?,?,?,?)",
        (new_id("aud"), user_id, action, resource, int(egress), now()),
    )


def get_active_subscription(conn: sqlite3.Connection, user_id: str, *, as_of: str | None = None):
    """取最新 active 且未过期订阅。period_end IS NULL 视为不过期 (单机长期授权/seed)。

    过滤 period_end 已过的 active 订阅 (避免过期档位继续放行); 无/过期 → None → 上层 free_local。
    """
    now_ts = as_of or now()
    return conn.execute(
        """SELECT s.sub_id, s.plan_code, s.status, s.period_start, s.period_end,
                  p.token_budget, p.rate_multiplier, p.name AS plan_name
           FROM subscriptions s JOIN plans p ON p.plan_code=s.plan_code
           WHERE s.user_id=? AND s.status='active'
             AND (s.period_end IS NULL OR s.period_end >= ?)
           ORDER BY s.period_start DESC LIMIT 1""",
        (user_id, now_ts)).fetchone()


# ---------- skills ----------

def upsert_skill(conn: sqlite3.Connection, *, name: str, source: str, origin_path: str, version: str,
                 tier: str, capability: dict, allowed_tools: list, auto_invoke: bool,
                 scan_status: str) -> str:
    row = conn.execute("SELECT skill_id FROM skills WHERE name=? AND source=?", (name, source)).fetchone()
    skill_id = row["skill_id"] if row else new_id("skill")
    conn.execute(
        """INSERT OR REPLACE INTO skills(skill_id,name,source,origin_path,version,tier,capability,
           allowed_tools,auto_invoke,enabled,scan_status,installed_at)
           VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
        (skill_id, name, source, origin_path, version, tier, jdump(capability),
         jdump(allowed_tools), int(auto_invoke), scan_status, now()),
    )
    return skill_id


def list_skills(conn: sqlite3.Connection) -> list[Row]:
    return conn.execute("SELECT * FROM skills WHERE enabled=1 ORDER BY installed_at DESC").fetchall()


def get_skill(conn: sqlite3.Connection, skill_id: str) -> Row | None:
    return conn.execute("SELECT * FROM skills WHERE skill_id=?", (skill_id,)).fetchone()


def record_skill_call(conn: sqlite3.Connection, *, user_id: str, skill_id: str, session_id: str | None,
                      chosen_model: str, tools_used: list, tokens_in: int, tokens_out: int,
                      cost_cny: float, latency_ms: int, status: str, egress: bool,
                      message_id: str | None = None, archived_doc_id: str | None = None) -> str:
    cid = new_id("skcall")
    conn.execute(
        """INSERT INTO skill_calls(call_id,user_id,skill_id,session_id,message_id,chosen_model,tools_used,
           tokens_in,tokens_out,cost_cny,latency_ms,status,egress_flag,archived_doc_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, user_id, skill_id, session_id, message_id, chosen_model, jdump(tools_used),
         tokens_in, tokens_out, cost_cny, latency_ms, status, int(egress), archived_doc_id, now()),
    )
    return cid


# ---------- Agentic 工具循环轨迹 (P0-A) ----------

def record_agent_step(conn: sqlite3.Connection, *, run_id: str, user_id: str, step_idx: int,
                      tool: str | None = None, parent_message_id: str | None = None,
                      args_hash: str | None = None, tokens_in: int = 0, tokens_out: int = 0,
                      latency_ms: int = 0, egress: bool = False, status: str = "ok") -> str:
    """记一步循环轨迹 (供 Inspector 渲染 + run 关联)。tool 为空表终答步。"""
    sid = new_id("step")
    conn.execute(
        """INSERT INTO agent_steps(step_id,run_id,user_id,parent_message_id,step_idx,tool,args_hash,
           tokens_in,tokens_out,latency_ms,egress,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, run_id, user_id, parent_message_id, step_idx, tool, args_hash,
         tokens_in, tokens_out, latency_ms, int(egress), status, now()),
    )
    return sid


def list_agent_steps(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """取某 run 的全部步骤, 按 step_idx 升序 (轨迹回放)。"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM agent_steps WHERE run_id=? ORDER BY step_idx", (run_id,)).fetchall()]


# ---------- 数据主权: 导出 / 级联删除 (§9 被遗忘权) ----------

def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def export_user_data(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """按 user_id 打包该用户全部数据为可序列化 dict (契约 D)。

    覆盖契约要求的 sessions/messages/vault_documents/doc_chunks/kg_entities/kg_edges/usage,
    并附带衍生表 (model_calls/embeddings/retrieval_logs/audit_log/skill_calls/subscriptions/licenses)
    以满足『打包该 user 全部数据』。messages/doc_chunks/embeddings 经父表 (sessions/documents) 归属该用户。
    """
    sessions = _rows(conn, "SELECT * FROM sessions WHERE user_id=? ORDER BY created_at", (user_id,))
    session_ids = [s["session_id"] for s in sessions]
    messages: list[dict[str, Any]] = []
    if session_ids:
        qmarks = ",".join("?" * len(session_ids))
        messages = _rows(
            conn,
            f"SELECT * FROM messages WHERE session_id IN ({qmarks}) ORDER BY created_at",
            tuple(session_ids),
        )

    documents = _rows(conn, "SELECT * FROM vault_documents WHERE user_id=? ORDER BY created_at", (user_id,))
    doc_ids = [d["doc_id"] for d in documents]
    chunks: list[dict[str, Any]] = []
    embeddings: list[dict[str, Any]] = []
    if doc_ids:
        qmarks = ",".join("?" * len(doc_ids))
        chunks = _rows(
            conn,
            f"SELECT * FROM doc_chunks WHERE doc_id IN ({qmarks}) ORDER BY doc_id,ordinal",
            tuple(doc_ids),
        )
        chunk_ids = [c["chunk_id"] for c in chunks]
        if chunk_ids:
            cmarks = ",".join("?" * len(chunk_ids))
            embeddings = _rows(
                conn,
                f"SELECT * FROM embeddings WHERE chunk_id IN ({cmarks})",
                tuple(chunk_ids),
            )

    return {
        "user": dict(get_user(conn, user_id) or {"user_id": user_id}),
        "sessions": sessions,
        "messages": messages,
        "model_calls": _rows(conn, "SELECT * FROM model_calls WHERE user_id=? ORDER BY created_at", (user_id,)),
        "vault_documents": documents,
        "doc_chunks": chunks,
        "embeddings": embeddings,
        "kg_entities": _rows(conn, "SELECT * FROM kg_entities WHERE user_id=?", (user_id,)),
        "kg_edges": _rows(conn, "SELECT * FROM kg_edges WHERE user_id=? ORDER BY asserted_at", (user_id,)),
        "usage": _rows(conn, "SELECT * FROM usage_ledger WHERE user_id=? ORDER BY period", (user_id,)),
        "retrieval_logs": _rows(conn, "SELECT * FROM retrieval_logs WHERE user_id=? ORDER BY created_at", (user_id,)),
        "audit_log": _rows(conn, "SELECT * FROM audit_log WHERE user_id=? ORDER BY created_at", (user_id,)),
        "skill_calls": _rows(conn, "SELECT * FROM skill_calls WHERE user_id=? ORDER BY created_at", (user_id,)),
        "agent_steps": _rows(conn, "SELECT * FROM agent_steps WHERE user_id=? ORDER BY run_id,step_idx", (user_id,)),
        "subscriptions": _rows(conn, "SELECT * FROM subscriptions WHERE user_id=?", (user_id,)),
        "licenses": _rows(conn, "SELECT * FROM licenses WHERE user_id=?", (user_id,)),
    }


def delete_user_data(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """级联删该 user_id 全表行 (契约 D)。返回各表删除行数。

    顺序遵守外键依赖: 先删子表 (经父表归属用户的 messages/doc_chunks/embeddings), 再删父表与用户行。
    调用方负责用 database.transaction 包裹以保证原子性。
    """
    counts: dict[str, int] = {}

    session_ids = [r["session_id"] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE user_id=?", (user_id,)).fetchall()]
    doc_ids = [r["doc_id"] for r in conn.execute(
        "SELECT doc_id FROM vault_documents WHERE user_id=?", (user_id,)).fetchall()]
    chunk_ids: list[str] = []
    if doc_ids:
        qmarks = ",".join("?" * len(doc_ids))
        chunk_ids = [r["chunk_id"] for r in conn.execute(
            f"SELECT chunk_id FROM doc_chunks WHERE doc_id IN ({qmarks})", tuple(doc_ids)).fetchall()]

    # 子表 (无 user_id 列, 经父表归属)
    if chunk_ids:
        cmarks = ",".join("?" * len(chunk_ids))
        counts["embeddings"] = conn.execute(
            f"DELETE FROM embeddings WHERE chunk_id IN ({cmarks})", tuple(chunk_ids)).rowcount
    if doc_ids:
        qmarks = ",".join("?" * len(doc_ids))
        counts["doc_chunks"] = conn.execute(
            f"DELETE FROM doc_chunks WHERE doc_id IN ({qmarks})", tuple(doc_ids)).rowcount
    # model_calls.message_id REFERENCES messages → 必须先于 messages 删 (否则 FK 违约, 被遗忘权失效)
    counts["model_calls"] = conn.execute(
        "DELETE FROM model_calls WHERE user_id=?", (user_id,)).rowcount
    if session_ids:
        smarks = ",".join("?" * len(session_ids))
        counts["messages"] = conn.execute(
            f"DELETE FROM messages WHERE session_id IN ({smarks})", tuple(session_ids)).rowcount

    # 带 user_id 列的表 (含 audit_log/retrieval_logs: 旧日志含用户查询正文, 一并抹除;
    # 删除后由调用方再写一条 delete 审计作为合规凭证, 故净留一条)
    # 顺序: kg_edges 先于 kg_entities/vault_documents; skill_calls 先于 vault_documents; model_calls 已先删。
    for table in ("agent_steps", "skill_calls", "kg_edges", "kg_entities", "retrieval_logs",
                  "audit_log", "usage_ledger", "subscriptions", "licenses", "vault_documents", "sessions"):
        counts[table] = conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,)).rowcount

    # 检索虚表 (含用户文档正文/查询词与向量): 被遗忘权须一并抹除。表缺 (FTS5/sqlite-vec 不可用) 则静默跳过。
    for vtable in ("chunks_fts", "vec_chunks"):
        try:
            counts[vtable] = conn.execute(f"DELETE FROM {vtable} WHERE user_id=?", (user_id,)).rowcount
        except sqlite3.OperationalError:
            counts[vtable] = 0

    counts["users"] = conn.execute("DELETE FROM users WHERE user_id=?", (user_id,)).rowcount
    return counts
