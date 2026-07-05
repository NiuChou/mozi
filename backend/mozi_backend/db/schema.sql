-- 墨子 · 数据库 Schema (SQLite 方言)
-- 关键表 DDL + Skill 兼容层。
-- 统一以 user_id 行级隔离 (数据主权)。迁移顺序化、启动时幂等执行。

PRAGMA foreign_keys = ON;

-- ========== 账户与订阅 ==========
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    email      TEXT UNIQUE NOT NULL,
    region     TEXT CHECK(region IN ('HK','CN')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plans (
    plan_code       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    price_cny       INTEGER NOT NULL,
    token_budget    INTEGER,           -- 周期 token 预算
    rate_multiplier REAL DEFAULT 1,    -- 用量倍率 (参考 Claude Max 5x/20x)
    features        TEXT               -- JSON
);

CREATE TABLE IF NOT EXISTS subscriptions (
    sub_id       TEXT PRIMARY KEY,
    user_id      TEXT REFERENCES users(user_id) ON DELETE CASCADE,
    plan_code    TEXT REFERENCES plans(plan_code) ON DELETE SET NULL,
    status       TEXT,                 -- active/past_due/canceled
    period_start TIMESTAMP,
    period_end   TIMESTAMP,
    seats        INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS licenses (
    license_id   TEXT PRIMARY KEY,
    user_id      TEXT REFERENCES users(user_id) ON DELETE CASCADE,
    device_tp    TEXT,
    activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ========== 会话与消息 ==========
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT REFERENCES users(user_id) ON DELETE CASCADE,
    title        TEXT,
    model_policy TEXT,
    archived     INTEGER NOT NULL DEFAULT 0,   -- 软归档: 1=移出主列表 (可恢复); 删除走硬删 (级联 messages)
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    session_id  TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
    role        TEXT,
    content_ref TEXT,                  -- 正文 (直存)
    model       TEXT,
    routing_meta TEXT,                 -- routing_metadata 快照 JSON, NULL=旧消息
    hits         TEXT,                 -- 去 text 的 Hit 引用数组 JSON
    usage_meta   TEXT,                 -- usage 快照 JSON
    injected     INTEGER DEFAULT 0,    -- 是否注入了接地上下文
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS model_calls (
    call_id       TEXT PRIMARY KEY,
    user_id       TEXT,
    message_id    TEXT REFERENCES messages(message_id) ON DELETE SET NULL,
    provider      TEXT,
    model         TEXT,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    cost_cny      REAL,
    latency_ms    INTEGER,
    strategy      TEXT,
    fallback_used INTEGER,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ========== 知识库文档与索引 ==========
CREATE TABLE IF NOT EXISTS vault_documents (
    doc_id       TEXT PRIMARY KEY,
    user_id      TEXT REFERENCES users(user_id) ON DELETE CASCADE,
    type         TEXT,                 -- 对话/笔记
    title        TEXT,
    crdt_state   BLOB,
    storage_mode TEXT,                 -- local/cloud/hybrid
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS doc_chunks (
    chunk_id    TEXT PRIMARY KEY,
    doc_id      TEXT REFERENCES vault_documents(doc_id) ON DELETE CASCADE,
    ordinal     INTEGER,
    text        TEXT,
    token_count INTEGER
);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id    TEXT PRIMARY KEY REFERENCES doc_chunks(chunk_id) ON DELETE CASCADE,
    vector      TEXT,                  -- JSON float[] 权威源 (vec_chunks 为可选 sqlite-vec 索引)
    embed_model TEXT,
    dim         INTEGER                -- 向量维度 (混维并存期按 embed_model 过滤)
);

-- ========== 知识图谱 Mozi-KG ==========
CREATE TABLE IF NOT EXISTS kg_entities (
    entity_id TEXT PRIMARY KEY,
    user_id   TEXT,
    type      TEXT,
    name      TEXT,
    aliases   TEXT,                    -- JSON
    embedding TEXT                     -- JSON float[]
);

CREATE TABLE IF NOT EXISTS kg_edges (
    edge_id       TEXT PRIMARY KEY,
    user_id       TEXT,
    subject_id    TEXT REFERENCES kg_entities(entity_id) ON DELETE CASCADE,
    predicate     TEXT,
    object_id     TEXT REFERENCES kg_entities(entity_id) ON DELETE CASCADE,
    valid_from    TIMESTAMP,           -- 双时序 (预留)
    valid_to      TIMESTAMP,
    asserted_at   TIMESTAMP NOT NULL,  -- 写入时刻 (必填)
    source_doc_id TEXT REFERENCES vault_documents(doc_id) ON DELETE SET NULL,
    confidence    REAL
);

-- ========== 用量计量与审计 ==========
CREATE TABLE IF NOT EXISTS usage_ledger (
    entry_id    TEXT PRIMARY KEY,
    user_id     TEXT,
    period      TEXT,
    tokens_used INTEGER,
    requests    INTEGER,
    cost_cny    REAL
);

CREATE TABLE IF NOT EXISTS retrieval_logs (
    log_id     TEXT PRIMARY KEY,
    user_id    TEXT,
    query      TEXT,
    routes     TEXT,                   -- JSON: 命中路由/chunk_ids
    latency_ms INTEGER,
    injected   INTEGER,                -- 是否注入对话
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- audit_log.action 约定: 'X.egress' = 出网物理事实, 即落即记 (egress_flag=1), 不随业务事务回滚;
--                        'X.persist' = 关联资源 (如 doc_id) 的可追溯行 (egress_flag=0, 不重复计出网)。
-- 前端审计视图: 出网计数看 *.egress, 文档可追溯看 *.persist。
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    TEXT PRIMARY KEY,
    user_id     TEXT,
    action      TEXT,
    resource    TEXT,
    egress_flag INTEGER,               -- egress_flag=1 标记数据出境
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ========== Skill 兼容层 (§8.5) ==========
CREATE TABLE IF NOT EXISTS skills (
    skill_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    source        TEXT,                -- claude/codex/mozi/git/pkg
    origin_path   TEXT,
    version       TEXT,
    tier          TEXT,                -- A/B/C 兼容等级
    capability    TEXT,                -- JSON: Capability Manifest(声明/推断)
    allowed_tools TEXT,                -- JSON
    auto_invoke   INTEGER DEFAULT 1,
    enabled       INTEGER DEFAULT 1,
    scan_status   TEXT,                -- 静态扫描结论
    installed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS skill_calls (
    call_id         TEXT PRIMARY KEY,
    user_id         TEXT,
    skill_id        TEXT REFERENCES skills(skill_id) ON DELETE SET NULL,
    session_id      TEXT,
    message_id      TEXT,
    chosen_model    TEXT,
    tools_used      TEXT,              -- JSON
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_cny        REAL,
    latency_ms      INTEGER,
    status          TEXT,
    egress_flag     INTEGER,
    archived_doc_id TEXT REFERENCES vault_documents(doc_id) ON DELETE SET NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ========== Agentic 工具循环轨迹 (P0-A) ==========
CREATE TABLE IF NOT EXISTS agent_steps (
    step_id           TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    user_id           TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    parent_message_id TEXT,
    step_idx          INTEGER NOT NULL,
    tool              TEXT,                -- 本步调用工具, 逗号拼接多个; 终答步为空
    args_hash         TEXT,
    tokens_in         INTEGER DEFAULT 0,
    tokens_out        INTEGER DEFAULT 0,
    latency_ms        INTEGER DEFAULT 0,
    egress            INTEGER DEFAULT 0,   -- 本步模型推理是否真实出网
    status            TEXT DEFAULT 'ok',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ========== 索引 ==========
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user      ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc         ON doc_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_docs_user          ON vault_documents(user_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_subject   ON kg_edges(subject_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_object    ON kg_edges(object_id);
CREATE INDEX IF NOT EXISTS idx_kg_entities_user   ON kg_entities(user_id);
CREATE INDEX IF NOT EXISTS idx_model_calls_user   ON model_calls(user_id);
CREATE INDEX IF NOT EXISTS idx_skill_calls_user   ON skill_calls(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_ledger_user   ON usage_ledger(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_ledger_period ON usage_ledger(user_id, period);
CREATE INDEX IF NOT EXISTS idx_retrieval_logs_user ON retrieval_logs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_user      ON audit_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_egress    ON audit_log(egress_flag);
CREATE INDEX IF NOT EXISTS idx_agent_steps_run     ON agent_steps(run_id);
