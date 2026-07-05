"""SQLite 连接与版本化迁移框架 (PRAGMA user_version + 内嵌顺序迁移)。

设计取向 (自研 ~框架, 零新增强依赖):
- 版本游标用 SQLite 原生 PRAGMA user_version。schema.sql = LATEST 基线 (全新库一次建到位),
  迁移把任意旧库逐级升到同一目标; 二者结构收敛。
- 核心不变量: 迁移事务内一律逐条 conn.execute, 绝不 executescript (隐式 COMMIT 会破坏
  transaction() 尾部 COMMIT 与 _set_user_version 的同事务原子性)。executescript 仅用于
  init_db 顶部跑 schema.sql (全 IF NOT EXISTS, 无 BEGIN/COMMIT, 不在 transaction 内)。
- PRAGMA foreign_keys 只在事务外切换 (SQLite 在 BEGIN 内切换被静默忽略)。
- 破坏性迁移前自动冷备份 DB 文件 (MOZI_MIGRATION_BACKUP_KEEP, 0=禁用)。
- 同机多进程首启争抢: advisory 文件锁 (fcntl) + 锁内 double-check user_version。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from ..config import settings
from ..util import jload, new_id, now

try:
    import fcntl  # POSIX advisory 锁; Windows 无 → 降级无锁
except ImportError:  # pragma: no cover - 仅非 POSIX
    fcntl = None  # type: ignore[assignment]

_LOG = logging.getLogger("mozi.migration")
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path or settings.db_path,
        check_same_thread=False,
        isolation_level=None,  # autocommit; 显式事务用 with conn
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


@contextmanager
def get_conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """每次请求开一个连接 (本地文件, 廉价)。"""
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        try:  # 关闭前清理 vec 扩展缓存 (防 id 复用误判已 load); 惰性 import 避免循环依赖
            from ..vault import vector_index
            vector_index._forget_conn(conn)
        except Exception:
            pass
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务: BEGIN → 成功 COMMIT, 异常 ROLLBACK 再 raise。

    连接为 autocommit (isolation_level=None), 故需显式 BEGIN。多写一致, 失败整体回滚。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ============================ 迁移注册结构 ============================
@dataclass(frozen=True)
class Migration:
    version: int                                    # apply 后 user_version=此值, 从1连续递增
    description: str                                # 中文说明, 打日志
    apply: Callable[[sqlite3.Connection], None]     # 在已 BEGIN 的事务内逐条 execute


def _get_user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _set_user_version(conn: sqlite3.Connection, v: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(v)}")  # PRAGMA 不接占位符, v 为可信内部整数


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _parent_pk(conn: sqlite3.Connection, parent: str) -> str:
    for r in conn.execute(f"PRAGMA table_info({parent})").fetchall():
        if r["pk"]:
            return r["name"]
    raise RuntimeError(f"父表 {parent} 无主键, 无法构造 FK")


def _audit_migration(conn: sqlite3.Connection, *, action: str, resource: str) -> None:
    """迁移内审计 (egress_flag=0, user_id='__system__')。复用 audit_log, 不经 egress 门。"""
    conn.execute(
        "INSERT INTO audit_log(audit_id,user_id,action,resource,egress_flag,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (new_id("aud"), "__system__", action, resource, 0, now()),
    )


# ---- 索引重放 (消解评审 MAJOR-丢索引: 表重建会带走全部索引) ----
def _parse_schema_indexes() -> dict[str, list[str]]:
    """从 schema.sql 解析 CREATE INDEX 语句, 按目标表名分组。模块加载时算一次。

    前提: 索引语句单行分号结尾 (test_schema_indexes_are_single_line 固化此约束)。
    """
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for stmt in re.findall(r"CREATE INDEX IF NOT EXISTS .*?;", text, re.S):
        m = re.search(r"\bON\s+(\w+)\s*\(", stmt)
        if m:
            out.setdefault(m.group(1), []).append(stmt.strip())
    return out


_SCHEMA_INDEXES = _parse_schema_indexes()


def _replay_indexes(conn: sqlite3.Connection, table: str) -> None:
    for stmt in _SCHEMA_INDEXES.get(table, []):
        conn.execute(stmt)                           # 逐条 execute, 绝不 executescript


# ---- 表重建 (优先 sqlite-utils.transform, 缺则自研 12 步) ----
def _rebuild_backend() -> str:
    return os.environ.get("MOZI_REBUILD_BACKEND", "auto")  # auto|sqlite_utils|builtin


def _use_sqlite_utils() -> bool:
    mode = _rebuild_backend()
    if mode == "builtin":
        return False
    if mode == "sqlite_utils":
        return True                                  # 强制; 缺库则 import 抛错暴露
    try:                                             # auto: 已装则用
        import sqlite_utils  # noqa: F401
        return True
    except ImportError:
        return False


def _new_ddl(conn: sqlite3.Connection, table: str, fks: list[tuple[str, str, str]]) -> str:
    """按 sqlite_master 实际列重建 CREATE TABLE (防列序漂移), FK 作表级约束补 ON DELETE。"""
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()  # cid,name,type,notnull,dflt,pk
    pk_cols = [r["name"] for r in info if r["pk"]]
    col_defs: list[str] = []
    for r in info:
        d = f'{r["name"]} {r["type"]}'.rstrip()
        if r["pk"] and len(pk_cols) == 1:
            d += " PRIMARY KEY"
        if r["notnull"]:
            d += " NOT NULL"
        if r["dflt_value"] is not None:
            d += f' DEFAULT {r["dflt_value"]}'
        col_defs.append(d)
    if len(pk_cols) > 1:
        col_defs.append(f'PRIMARY KEY({", ".join(pk_cols)})')
    for col, parent, action in fks:
        col_defs.append(
            f"FOREIGN KEY({col}) REFERENCES {parent}({_parent_pk(conn, parent)}) ON DELETE {action}"
        )
    body = ",\n  ".join(col_defs)
    return f"CREATE TABLE {table} (\n  {body}\n)"


def _rebuild_table(conn: sqlite3.Connection, table: str, fks: list[tuple[str, str, str]]) -> None:
    """SQLite 高级 ALTER 模式 (改 FK)。调用前 FK 已 OFF (由 _run_migrations 保证)。"""
    columns = _table_columns(conn, table)
    if _use_sqlite_utils():
        import sqlite_utils
        db = sqlite_utils.Database(conn)
        db[table].transform(foreign_keys=[(c, p, _parent_pk(conn, p)) for c, p, _ in fks])
    else:
        new_ddl = _new_ddl(conn, table, fks)         # ★须在 RENAME 前算 (RENAME 后 table_info 为空)
        tmp = f"_{table}_old"
        conn.execute(f"ALTER TABLE {table} RENAME TO {tmp}")
        conn.execute(new_ddl)
        cols = ", ".join(columns)                    # 按列名拷贝, 防列序漂移
        conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {tmp}")
        conn.execute(f"DROP TABLE {tmp}")
    _replay_indexes(conn, table)                     # 重建必重放索引


# ============================ 迁移 v1: 补 FK ON DELETE ============================
_M001_TABLES: dict[str, list[tuple[str, str, str]]] = {
    "subscriptions":   [("user_id", "users", "CASCADE"), ("plan_code", "plans", "SET NULL")],
    "licenses":        [("user_id", "users", "CASCADE")],
    "sessions":        [("user_id", "users", "CASCADE")],
    "messages":        [("session_id", "sessions", "CASCADE")],
    "model_calls":     [("message_id", "messages", "SET NULL")],   # 计费历史保留
    "vault_documents": [("user_id", "users", "CASCADE")],
    "doc_chunks":      [("doc_id", "vault_documents", "CASCADE")],
    "embeddings":      [("chunk_id", "doc_chunks", "CASCADE")],
    "kg_edges":        [("subject_id", "kg_entities", "CASCADE"),
                        ("object_id", "kg_entities", "CASCADE"),
                        ("source_doc_id", "vault_documents", "SET NULL")],
    "skill_calls":     [("skill_id", "skills", "SET NULL"),
                        ("archived_doc_id", "vault_documents", "SET NULL")],
}


def _needs_rebuild(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None and "ON DELETE" not in (row[0] or "")


def _on_dirty() -> str:
    return os.environ.get("MOZI_MIGRATION_ON_DIRTY", "purge")  # purge|skip_table|raise


def _sanitize_orphans(conn: sqlite3.Connection, table: str, fks: list[tuple[str, str, str]]) -> None:
    """重建前隔离悬空 FK: SET NULL 列置 NULL, CASCADE 列删孤儿行并审计 (FK=OFF 状态执行)。"""
    if _on_dirty() == "raise":
        return                                       # 交给重建后 foreign_key_check 暴露
    for col, parent, action in fks:
        pk = _parent_pk(conn, parent)
        cond = (f"{col} IS NOT NULL AND NOT EXISTS "
                f"(SELECT 1 FROM {parent} p WHERE p.{pk}={table}.{col})")
        n = conn.execute(f"SELECT count(*) FROM {table} WHERE {cond}").fetchone()[0]
        if n == 0:
            continue
        if action == "SET NULL":
            conn.execute(f"UPDATE {table} SET {col}=NULL WHERE {cond}")
        else:                                        # CASCADE: 删孤儿行
            conn.execute(f"DELETE FROM {table} WHERE {cond}")
        _audit_migration(conn, action="migration_orphan_purge", resource=f"{table}.{col}:{action}:{n}")


def _m001_fk_on_delete(conn: sqlite3.Connection) -> None:
    for table, fks in _M001_TABLES.items():
        if not _needs_rebuild(conn, table):          # 幂等: 已含 ON DELETE 则跳过
            continue
        _sanitize_orphans(conn, table, fks)
        _rebuild_table(conn, table, fks)
    for table in _M001_TABLES:                       # 兜底统一重放 (IF NOT EXISTS 安全)
        _replay_indexes(conn, table)


# ============================ 迁移 v2: 审计/用量索引 (逐条 execute) ============================
_M002_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_usage_ledger_user   ON usage_ledger(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_ledger_period ON usage_ledger(user_id, period)",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_logs_user ON retrieval_logs(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_user      ON audit_log(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_egress    ON audit_log(egress_flag)",
]


def _m002_audit_indexes(conn: sqlite3.Connection) -> None:
    for stmt in _M002_INDEXES:                       # 逐条, 不 executescript
        conn.execute(stmt)


def _m003_embeddings_dim(conn: sqlite3.Connection) -> None:
    """embeddings 加 dim 列 (混维过滤用) + 回填旧向量维度。逐条 execute。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(embeddings)")}
    if "dim" not in cols:
        conn.execute("ALTER TABLE embeddings ADD COLUMN dim INTEGER")
    for r in conn.execute("SELECT chunk_id, vector FROM embeddings WHERE dim IS NULL").fetchall():
        d = len(jload(r["vector"], [])) or settings.embed_dim   # WHERE dim IS NULL: 只回填一次
        conn.execute("UPDATE embeddings SET dim=? WHERE chunk_id=?", (d, r["chunk_id"]))


def _m004_message_metadata(conn: sqlite3.Connection) -> None:
    """messages 加 per-message 元数据列。逐条 ALTER (PRAGMA table_info 守卫幂等), 禁 executescript。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    for name, decl in (("routing_meta", "TEXT"), ("hits", "TEXT"),
                       ("usage_meta", "TEXT"), ("injected", "INTEGER DEFAULT 0")):
        if name not in have:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {decl}")


def _m005_agent_steps(conn: sqlite3.Connection) -> None:
    """agentic 工具循环轨迹表 (P0-A)。已部署 v4 库无此表 → 建表 + 索引 (IF NOT EXISTS 幂等)。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_steps ("
        "step_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE, "
        "parent_message_id TEXT, step_idx INTEGER NOT NULL, tool TEXT, args_hash TEXT, "
        "tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0, "
        "egress INTEGER DEFAULT 0, status TEXT DEFAULT 'ok', "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id)")


def _m006_session_archived(conn: sqlite3.Connection) -> None:
    """sessions 加 archived 列 (软归档)。PRAGMA table_info 守卫幂等, 禁 executescript。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "archived" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")


MIGRATIONS: list[Migration] = [
    Migration(1, "补 FK ON DELETE 子句 (表重建+孤儿清理+重放索引)", _m001_fk_on_delete),
    Migration(2, "usage_ledger/retrieval_logs/audit_log 行级隔离+时间索引", _m002_audit_indexes),
    Migration(3, "embeddings 加 dim 列 + 回填 (混维过滤)", _m003_embeddings_dim),
    Migration(4, "messages per-message 元数据列 (routing_meta/hits/usage_meta/injected)", _m004_message_metadata),
    Migration(5, "agent_steps 表 (agentic 工具循环轨迹, P0-A)", _m005_agent_steps),
    Migration(6, "sessions 加 archived 列 (会话软归档)", _m006_session_archived),
]
LATEST_VERSION = max((m.version for m in MIGRATIONS), default=0)


def _ensure_search_tables(conn: sqlite3.Connection) -> None:
    """检索虚表 (FTS5/vec) 条件创建。fresh+legacy 都调用以收敛 (虚表不进 schema.sql 基线)。

    缺 FTS5 / 缺 sqlite-vec 时静默跳过 (幂等, IF NOT EXISTS), 降级运行不受影响。
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                     "chunk_id UNINDEXED, user_id UNINDEXED, text, "
                     "tokenize='unicode61 remove_diacritics 2')")
    except sqlite3.OperationalError:
        pass
    try:
        from ..vault import embedder, vector_index
        vector_index.ensure_index(conn, embedder.active_dim())
    except Exception:
        pass


# ============================ 备份 / advisory 锁 ============================
def _prune_backups(bdir: Path, db_name: str, keep: int) -> None:
    backups = sorted(bdir.glob(f"{db_name}.v*.bak"), key=lambda p: p.stat().st_mtime_ns)
    for old in backups[:-keep] if keep > 0 else backups:
        old.unlink(missing_ok=True)


def _backup_db(conn: sqlite3.Connection, target, pre_version: int) -> Path | None:
    """破坏性迁移前冷备份。返回备份路径; 内存库/禁用/不存在时 None no-op。"""
    keep = int(os.environ.get("MOZI_MIGRATION_BACKUP_KEEP", "5"))
    if keep <= 0 or target is None or str(target) == ":memory:":
        return None
    target = Path(target)
    if not target.exists():
        return None
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 把 -wal 落主库再整文件冷拷
    except sqlite3.OperationalError:
        pass
    bdir = target.parent / "migration_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / f"{target.name}.v{pre_version}.{time.strftime('%Y%m%d%H%M%S')}.{uuid.uuid4().hex[:8]}.bak"
    shutil.copy(target, backup)                      # 不拷元数据, 保留新 mtime 供 prune 排序
    _prune_backups(bdir, target.name, keep)
    _audit_migration(conn, action="migration_backup", resource=backup.name)
    return backup


def restore_from_backup(backup, db_path=None) -> None:
    """停服后用备份替换主库 (运维显式调用)。清理 -wal/-shm sidecar。"""
    target = Path(db_path) if db_path is not None else settings.db_path
    for suffix in ("-wal", "-shm"):
        p = Path(str(target) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(backup, target)


@contextmanager
def _migration_lock(target) -> Iterator[None]:
    """迁移期 advisory 互斥。同机多进程首启仅一个执行, 余者阻塞后看到 user_version==LATEST 跳过。

    内存库/无 fcntl(Windows): 降级无锁 (单进程语义不变)。
    """
    if fcntl is None or target is None or str(target) == ":memory:":
        yield
        return
    lock_path = Path(str(target) + ".miglock")
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


# ============================ init / 版本推进 ============================
def _run_migrations(conn: sqlite3.Connection, current: int) -> None:
    for m in sorted(MIGRATIONS, key=lambda x: x.version):
        if m.version <= current:
            continue
        conn.execute("PRAGMA foreign_keys = OFF")    # PRAGMA 必须在 BEGIN 之外
        try:
            with transaction(conn):                  # 失败整体 ROLLBACK, 版本与 DDL 同事务原子
                m.apply(conn)
                _set_user_version(conn, m.version)
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            _handle_dirty_fk(conn, m.version, bad)   # 按 ON_DIRTY 降级, 不无条件 brick
        _LOG.info("migration v%d applied: %s", m.version, m.description)


def _handle_dirty_fk(conn: sqlite3.Connection, version: int, bad: list) -> None:
    if _on_dirty() == "raise":
        raise RuntimeError(f"迁移 v{version} 后 FK 完整性校验失败: {bad}")
    _audit_migration(conn, action="migration_fk_residual", resource=str(bad[:20]))


def init_db(db_path: Path | None = None) -> None:
    """建库 + 顺序应用迁移。幂等。签名不变。"""
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    target = db_path if db_path is not None else settings.db_path
    with get_conn(db_path) as conn:
        fresh = not _table_exists(conn, "users")     # users 表作探针
        conn.executescript(ddl)                      # 唯一允许 executescript 处 (schema.sql 全 IF NOT EXISTS)
        if fresh and _get_user_version(conn) == 0:
            _set_user_version(conn, LATEST_VERSION)  # 全新库直达 LATEST
        elif _get_user_version(conn) < LATEST_VERSION:
            with _migration_lock(target):
                current = _get_user_version(conn)    # 锁内 double-check (等锁期间他进程可能已迁完)
                if current < LATEST_VERSION:
                    _backup_db(conn, target, current)    # 破坏性迁移前自动备份
                    _run_migrations(conn, current)
        # 检索虚表 (FTS5/vec) 始终幂等确保: fresh 库跳过迁移仍须建表, 与升级库收敛
        _ensure_search_tables(conn)


def reset_db(db_path: Path | None = None) -> None:
    """删库重建 (测试/开发用)。"""
    target = db_path or settings.db_path
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(target) + suffix)
        if p.exists():
            p.unlink()
    init_db(db_path)
