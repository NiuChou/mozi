"""EPIC-MIGRATION 回归: PRAGMA user_version 框架 + v3 深化 (备份/transform/索引守卫/并发锁)。

覆盖评审 blocker/major/minor 与 v3 open_risks 落定。零 key 零外呼, 每用例独立临时库。
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

try:  # 兼容 `discover -s tests` (顶层) 与 `unittest tests.X` (包) 两种运行方式
    from ._helpers import TMP_DIR
except ImportError:
    from _helpers import TMP_DIR

from mozi_backend.db import dal, database  # noqa: E402
from mozi_backend.db.database import (  # noqa: E402
    LATEST_VERSION,
    Migration,
    SCHEMA_PATH,
    _get_user_version,
    init_db,
)

_NEW_INDEXES = (
    "idx_usage_ledger_user", "idx_usage_ledger_period", "idx_retrieval_logs_user",
    "idx_audit_log_user", "idx_audit_log_egress",
)


def _legacy_ddl() -> str:
    """由当前 schema.sql 反推 v0 旧结构: 去 ON DELETE 子句 + 去 v2 新增索引 + 去后续迁移新增表。

    agent_steps 是 v5 才引入的表 → 旧库须无此表/索引, 迁移 v5 才真正 CREATE (否则 IF NOT EXISTS
    永不触发, fresh vs upgraded 的 FK/列分歧将逃过测试)。
    """
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    text = re.sub(r" ON DELETE (CASCADE|SET NULL)", "", text)
    for name in _NEW_INDEXES:
        text = re.sub(rf"CREATE INDEX IF NOT EXISTS {name}\b[^;]*;\n?", "", text)
    # v5+ 新增表/索引: 旧库不含, 交由迁移建 (否则 IF NOT EXISTS 形同虚设)
    text = re.sub(r"CREATE TABLE IF NOT EXISTS agent_steps\b.*?\);\n?", "", text, flags=re.S)
    text = re.sub(r"CREATE INDEX IF NOT EXISTS idx_agent_steps_run\b[^;]*;\n?", "", text)
    return text


def _new_db_path() -> str:
    fd, path = tempfile.mkstemp(prefix="mozi_mig_", suffix=".db", dir=TMP_DIR)
    os.close(fd)
    os.unlink(path)  # 交给 init_db / fixture 自建
    return path


def _make_legacy_db(path: str, *, with_data: bool = True, orphan: bool = False) -> None:
    """建一个 user_version=0 的旧结构库 (无 ON DELETE / 无审计索引)。"""
    conn = sqlite3.connect(path)
    conn.executescript(_legacy_ddl())
    conn.execute("PRAGMA foreign_keys = OFF")  # 旧库/孤儿来源: 历史 FK=OFF 写入
    conn.execute("PRAGMA user_version = 0")
    if with_data:
        conn.execute("INSERT INTO users(user_id,email,region) VALUES('u1','u1@x.cn','CN')")
        conn.execute("INSERT INTO sessions(session_id,user_id,title) VALUES('s1','u1','t')")
        conn.execute("INSERT INTO messages(message_id,session_id,role,content_ref) "
                     "VALUES('m1','s1','user','hi')")
        conn.execute("INSERT INTO messages(message_id,session_id,role,content_ref) "
                     "VALUES('m2','s1','assistant','yo')")
        conn.execute("INSERT INTO model_calls(call_id,user_id,message_id,provider,model) "
                     "VALUES('c1','u1','m2','mock','mock')")
        conn.execute("INSERT INTO vault_documents(doc_id,user_id,type,title) "
                     "VALUES('d1','u1','note','doc')")
        conn.execute("INSERT INTO doc_chunks(chunk_id,doc_id,ordinal,text) "
                     "VALUES('ch1','d1',0,'body')")
        conn.execute("INSERT INTO embeddings(chunk_id,vector,embed_model) "
                     "VALUES('ch1','[0.0]','mock')")
    if orphan:
        conn.execute("INSERT INTO sessions(session_id,user_id,title) VALUES('sx','ghost','orphan')")
    conn.commit()
    conn.close()


def _index_names(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'").fetchall()}


def _set_env(case: unittest.TestCase, **kv: str | None) -> None:
    """临时设环境变量, 用例结束自动还原。"""
    for k, v in kv.items():
        old = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

        def _restore(k: str = k, old: str | None = old) -> None:
            if old is not None:
                os.environ[k] = old
            else:
                os.environ.pop(k, None)

        case.addCleanup(_restore)


class FreshDbTest(unittest.TestCase):
    def test_fresh_db_at_latest_version(self):
        path = _new_db_path()
        init_db(path)
        conn = sqlite3.connect(path)
        self.assertEqual(_get_user_version(conn), LATEST_VERSION)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        self.assertIn("ON DELETE", sql)
        conn.close()

    def test_schema_indexes_are_single_line(self):
        """重放正则前提: CREATE INDEX 必须单行分号结尾 (多行会被漏解析)。"""
        for line in SCHEMA_PATH.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.upper().startswith("CREATE INDEX"):
                self.assertTrue(s.endswith(";"), f"索引须单行分号结尾: {s}")

    def test_upgraded_indexes_match_fresh(self):
        fresh = _new_db_path()
        init_db(fresh)
        legacy = _new_db_path()
        _make_legacy_db(legacy)
        init_db(legacy)
        cf = sqlite3.connect(fresh)
        cl = sqlite3.connect(legacy)
        self.assertEqual(_index_names(cf), _index_names(cl))
        cf.close()
        cl.close()


def _columns(conn, table) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


class SchemaConvergeTest(unittest.TestCase):
    def test_messages_columns_converge(self):
        """全新库 vs 空 messages 升级库: 列集合一致且含元数据 + ON DELETE CASCADE。"""
        fresh = _new_db_path()
        init_db(fresh)
        legacy = _new_db_path()
        _make_legacy_db(legacy, with_data=False)
        init_db(legacy)
        cf = sqlite3.connect(fresh)
        cl = sqlite3.connect(legacy)
        self.assertEqual(_columns(cf, "messages"), _columns(cl, "messages"))
        for col in ("routing_meta", "hits", "usage_meta", "injected"):
            self.assertIn(col, _columns(cl, "messages"), f"升级库 messages 须含 {col}")
        self.assertIn("ON DELETE", cl.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0])
        cf.close()
        cl.close()

    def test_agent_steps_converges_fresh_vs_upgraded(self):
        """全新库 vs v5 升级库: agent_steps 列集合一致且均含 FK ON DELETE CASCADE (无 FK 分歧)。"""
        fresh = _new_db_path()
        init_db(fresh)
        legacy = _new_db_path()
        _make_legacy_db(legacy, with_data=False)   # 旧库无 agent_steps → v5 建表
        init_db(legacy)
        cf = sqlite3.connect(fresh)
        cl = sqlite3.connect(legacy)
        self.assertEqual(_columns(cf, "agent_steps"), _columns(cl, "agent_steps"))
        for c in (cf, cl):
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='agent_steps'").fetchone()[0]
            self.assertIn("ON DELETE CASCADE", sql)
        cf.close()
        cl.close()


class LegacyUpgradeTest(unittest.TestCase):
    def test_legacy_db_upgrades(self):
        path = _new_db_path()
        _make_legacy_db(path)
        init_db(path)
        conn = sqlite3.connect(path)
        self.assertEqual(_get_user_version(conn), LATEST_VERSION)
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT count(*) FROM embeddings").fetchone()[0], 1)
        self.assertIn("ON DELETE", conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0])
        self.assertIn("idx_audit_log_user", _index_names(conn))
        conn.close()

    def test_migration_no_executescript_commit_crash(self):
        """消解 BLOCKER: v1+v2 升级全程不抛 'cannot commit - no transaction is active'。"""
        path = _new_db_path()
        _make_legacy_db(path)
        init_db(path)  # 不抛即通过
        conn = sqlite3.connect(path)
        self.assertEqual(_get_user_version(conn), LATEST_VERSION)
        conn.close()

    def test_migration_idempotent(self):
        path = _new_db_path()
        _make_legacy_db(path)
        init_db(path)
        c = sqlite3.connect(path)
        v0 = _get_user_version(c)
        rows0 = c.execute("SELECT count(*) FROM messages").fetchone()[0]
        idx0 = len(_index_names(c))
        c.close()
        init_db(path)  # 再跑
        c = sqlite3.connect(path)
        self.assertEqual(_get_user_version(c), v0)
        self.assertEqual(c.execute("SELECT count(*) FROM messages").fetchone()[0], rows0)
        self.assertEqual(len(_index_names(c)), idx0)
        c.close()

    def test_foreign_key_check_clean(self):
        path = _new_db_path()
        _make_legacy_db(path)
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.close()

    def test_fk_cascade_behavior(self):
        path = _new_db_path()
        _make_legacy_db(path)
        init_db(path)
        conn = database._connect(path)  # FK=ON
        with database.transaction(conn):
            conn.execute("DELETE FROM users WHERE user_id='u1'")
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM sessions WHERE user_id='u1'").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM messages WHERE session_id='s1'").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM vault_documents WHERE user_id='u1'").fetchone()[0], 0)
        row = conn.execute("SELECT message_id FROM model_calls WHERE call_id='c1'").fetchone()
        self.assertIsNotNone(row)         # 计费历史保留
        self.assertIsNone(row[0])         # message_id SET NULL
        conn.close()


class OrphanTest(unittest.TestCase):
    def test_orphan_purge_no_brick(self):
        path = _new_db_path()
        _make_legacy_db(path, orphan=True)
        init_db(path)  # 默认 purge, 不抛
        conn = sqlite3.connect(path)
        self.assertEqual(_get_user_version(conn), LATEST_VERSION)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM sessions WHERE user_id='ghost'").fetchone()[0], 0)
        self.assertGreaterEqual(conn.execute(
            "SELECT count(*) FROM audit_log WHERE action='migration_orphan_purge'").fetchone()[0], 1)
        conn.close()

    def test_on_dirty_raise_mode(self):
        _set_env(self, MOZI_MIGRATION_ON_DIRTY="raise")
        path = _new_db_path()
        _make_legacy_db(path, orphan=True)
        with self.assertRaises(RuntimeError):
            init_db(path)
        conn = sqlite3.connect(path)
        self.assertLess(_get_user_version(conn), LATEST_VERSION)  # 未完成迁移
        conn.close()


class RebuildBackendTest(unittest.TestCase):
    def test_rebuild_fallback_without_sqlite_utils(self):
        _set_env(self, MOZI_REBUILD_BACKEND="builtin")
        path = _new_db_path()
        _make_legacy_db(path)
        init_db(path)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
        self.assertIn("idx_sessions_user", _index_names(conn))  # 重建后索引重放
        conn.close()

    def test_rebuild_via_sqlite_utils(self):
        try:
            import sqlite_utils  # noqa: F401
        except ImportError:
            self.skipTest("sqlite-utils 未安装 (可选优化路径)")
        _set_env(self, MOZI_REBUILD_BACKEND="sqlite_utils")
        path = _new_db_path()
        _make_legacy_db(path)
        init_db(path)
        conn = sqlite3.connect(path)
        self.assertEqual(_get_user_version(conn), LATEST_VERSION)
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
        conn.close()


class RollbackTest(unittest.TestCase):
    def test_migration_rollback(self):
        path = _new_db_path()
        _make_legacy_db(path)
        conn = database._connect(path)

        def boom(c: sqlite3.Connection) -> None:
            c.execute("CREATE TABLE _tmp_boom(x)")
            raise ValueError("boom")

        orig = database.MIGRATIONS
        database.MIGRATIONS = [Migration(1, "boom", boom)]
        try:
            with self.assertRaises(ValueError):
                database._run_migrations(conn, 0)
            self.assertEqual(_get_user_version(conn), 0)                  # 版本未推进
            self.assertFalse(database._table_exists(conn, "_tmp_boom"))   # 事务回滚
        finally:
            database.MIGRATIONS = orig
            conn.close()


class DeleteUserDataTest(unittest.TestCase):
    def test_delete_user_data_rowcounts_intact(self):
        """消解 MINOR: 新 schema(含 CASCADE) 下 delete_user_data 各表 rowcount 不被级联抢零。"""
        path = _new_db_path()
        _make_legacy_db(path)
        init_db(path)
        conn = database._connect(path)
        with database.transaction(conn):
            counts = dal.delete_user_data(conn, "u1")
        self.assertEqual(counts["messages"], 2)
        self.assertEqual(counts["sessions"], 1)
        self.assertEqual(counts["vault_documents"], 1)
        self.assertEqual(counts["users"], 1)
        conn.close()


class BackupTest(unittest.TestCase):
    def test_backup_created_before_destructive_migration(self):
        path = _new_db_path()
        _make_legacy_db(path)
        init_db(path)
        bdir = Path(path).parent / "migration_backups"
        baks = list(bdir.glob(f"{Path(path).name}.v*.bak"))
        self.assertGreaterEqual(len(baks), 1)
        conn = sqlite3.connect(path)
        self.assertGreaterEqual(conn.execute(
            "SELECT count(*) FROM audit_log WHERE action='migration_backup'").fetchone()[0], 1)
        conn.close()

    def test_backup_pruned_to_keep_n(self):
        _set_env(self, MOZI_MIGRATION_BACKUP_KEEP="2")
        path = _new_db_path()
        init_db(path)  # fresh, 已 LATEST
        conn = database._connect(path)
        for i in range(4):
            database._backup_db(conn, path, i)
        bdir = Path(path).parent / "migration_backups"
        baks = list(bdir.glob(f"{Path(path).name}.v*.bak"))
        self.assertEqual(len(baks), 2)
        conn.close()

    def test_memory_db_no_backup_noop(self):
        conn = database._connect(":memory:")
        self.assertIsNone(database._backup_db(conn, ":memory:", 0))
        self.assertIsNone(database._backup_db(conn, None, 0))
        conn.close()

    def test_restore_from_backup_roundtrip(self):
        path = _new_db_path()
        init_db(path)
        conn = database._connect(path)
        conn.execute("INSERT INTO users(user_id,email,region) VALUES('keep','k@x.cn','CN')")
        backup = database._backup_db(conn, path, 0)
        conn.execute("DELETE FROM users WHERE user_id='keep'")
        conn.close()
        self.assertIsNotNone(backup)
        database.restore_from_backup(backup, path)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM users WHERE user_id='keep'").fetchone()[0], 1)
        conn.close()


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_first_start_single_migration(self):
        path = _new_db_path()
        _make_legacy_db(path)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                init_db(path)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        conn = sqlite3.connect(path)
        self.assertEqual(_get_user_version(conn), LATEST_VERSION)
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
        # advisory 锁 + double-check: 仅一个线程真正迁移 → 仅一条备份审计
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM audit_log WHERE action='migration_backup'").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
