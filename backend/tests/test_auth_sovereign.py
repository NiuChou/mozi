"""契约 A (auth) + 契约 D (#9 export/delete) + #7 multiuser 经 API。

auth: 默认模式恒返回 default_user; require_auth=1 校验 Bearer device_token;
      multiuser=1 + X-User-Id → 该 user。
export/delete: 建数据后 export 含 sessions/vault; delete 后再查为空 + 写 delete 审计。
"""
from __future__ import annotations

import dataclasses
import os
import sqlite3
import unittest

from fastapi import HTTPException

try:  # 兼容 `discover -s tests` (顶层) 与 `unittest tests.X` (包) 两种运行方式
    from ._helpers import _auth_headers, fresh_conn
except ImportError:
    from _helpers import _auth_headers, fresh_conn

from mozi_backend import auth  # noqa: E402
from mozi_backend.db import dal  # noqa: E402
from mozi_backend.db.database import transaction  # noqa: E402
from mozi_backend.vault import service  # noqa: E402


class _SettingsCtx:
    """临时替换 auth.settings 为指定开关, 退出时还原 (frozen dataclass 安全)。

    用 dataclasses.replace 透传任意字段 (含 multiuser_allow_unsigned/user_token_ttl_sec/
    allow_legacy_v1), 消解评审 MAJOR-2 (旧实现只透传 2 字段, 新开关恒取 env 默认测不到)。
    """

    def __init__(self, **overrides) -> None:
        self.overrides = overrides

    def __enter__(self):
        self._orig = auth.settings
        new = dataclasses.replace(auth.settings, **self.overrides)
        auth.settings = new
        return new

    def __exit__(self, *a):
        auth.settings = self._orig


class AuthDependencyTest(unittest.TestCase):
    def test_default_mode_returns_default_user(self) -> None:
        with _SettingsCtx(require_auth=False, multiuser=False):
            uid = auth.current_user_id(x_user_id="ignored", authorization=None)
            self.assertEqual(uid, auth.settings.default_user_id,
                             "默认模式忽略 X-User-Id, 恒返回 default_user (旧行为)")

    def test_multiuser_honors_signed_token(self) -> None:
        with _SettingsCtx(require_auth=False, multiuser=True):
            auth._DEVICE_TOKEN = "unit-secret"
            try:
                tok = auth.sign_user_token("alice")
                self.assertEqual(
                    auth.current_user_id(x_user_id=None, authorization=f"Bearer {tok}"), "alice")
                # 带一致 X-User-Id 也放行
                self.assertEqual(
                    auth.current_user_id(x_user_id="alice", authorization=f"Bearer {tok}"), "alice")
                # 裸 X-User-Id 无令牌 + allow_unsigned=0 → 401 (收紧点)
                with self.assertRaises(HTTPException) as ctx:
                    auth.current_user_id(x_user_id="alice", authorization=None)
                self.assertEqual(ctx.exception.status_code, 401)
            finally:
                auth._DEVICE_TOKEN = None

    def test_require_auth_rejects_bad_token(self) -> None:
        with _SettingsCtx(require_auth=True, multiuser=False):
            # 注入已知 device_token
            auth._DEVICE_TOKEN = "good-token"
            try:
                with self.assertRaises(HTTPException) as ctx:
                    auth.current_user_id(x_user_id=None, authorization="Bearer wrong")
                self.assertEqual(ctx.exception.status_code, 401)
                with self.assertRaises(HTTPException):
                    auth.current_user_id(x_user_id=None, authorization=None)
                # 正确 token 放行
                uid = auth.current_user_id(x_user_id=None, authorization="Bearer good-token")
                self.assertEqual(uid, auth.settings.default_user_id)
            finally:
                auth._DEVICE_TOKEN = None

    def test_require_auth_plus_multiuser(self) -> None:
        # multiuser=1 下身份恒由 user_token 承载 (require_auth 的 device-token 路仅作用于单用户模式)
        with _SettingsCtx(require_auth=True, multiuser=True):
            auth._DEVICE_TOKEN = "tok"
            try:
                tok = auth.sign_user_token("bob")
                uid = auth.current_user_id(x_user_id="bob", authorization=f"Bearer {tok}")
                self.assertEqual(uid, "bob", "合法 user_token 取签名 uid")
            finally:
                auth._DEVICE_TOKEN = None


class ExportDeleteTest(unittest.TestCase):
    def _seed_user(self, conn, uid="u_demo"):
        dal.ensure_user(conn, uid, f"{uid}@mozi.local")
        sid = dal.create_session(conn, uid, "会话1", "auto")
        dal.add_message(conn, sid, "user", "墨子是什么？")
        dal.add_message(conn, sid, "assistant", "墨子是本地优先应用。", model="glm-5.2")
        service.archive_document(conn, user_id=uid, title="笔记",
                                 content="墨子使用 SQLite。\nUMA 是路由网关。")
        dal.record_model_call(conn, user_id=uid, message_id=None, provider="glm", model="glm-5.2",
                              tokens_in=10, tokens_out=20, cost_cny=0.001, latency_ms=5,
                              strategy="balanced", fallback_used=False)
        return sid

    def test_export_contains_sessions_and_vault(self) -> None:
        conn = fresh_conn()
        try:
            self._seed_user(conn)
            data = dal.export_user_data(conn, "u_demo")
            self.assertEqual(len(data["sessions"]), 1, "export 须含 session")
            self.assertEqual(len(data["messages"]), 2, "export 须含 messages")
            self.assertTrue(data["vault_documents"], "export 须含 vault 文档")
            self.assertTrue(data["doc_chunks"], "export 须含 chunks")
            self.assertTrue(data["kg_entities"], "export 须含 KG 实体")
            self.assertTrue(data["kg_edges"], "export 须含 KG 边")
            self.assertTrue(data["usage"], "export 须含用量")
            self.assertTrue(data["model_calls"], "export 须含模型调用")
        finally:
            conn.close()

    def test_delete_cascades_to_empty_and_audits(self) -> None:
        conn = fresh_conn()
        try:
            self._seed_user(conn)
            with transaction(conn):
                counts = dal.delete_user_data(conn, "u_demo")
                dal.log_audit(conn, user_id="u_demo", action="delete", resource="account", egress=False)
            # 删除后 export 全空
            after = dal.export_user_data(conn, "u_demo")
            self.assertEqual(after["sessions"], [])
            self.assertEqual(after["messages"], [])
            self.assertEqual(after["vault_documents"], [])
            self.assertEqual(after["doc_chunks"], [])
            self.assertEqual(after["kg_entities"], [])
            self.assertEqual(after["kg_edges"], [])
            self.assertEqual(after["usage"], [])
            self.assertEqual(after["model_calls"], [])
            # 删除计数覆盖关键表
            self.assertGreaterEqual(counts.get("sessions", 0), 1)
            self.assertGreaterEqual(counts.get("vault_documents", 0), 1)
            # 被遗忘权: 检索虚表 (FTS 正文/向量) 亦须抹除, 否则用户文档正文残留 (FTS5 可用时校验)
            try:
                fts_left = conn.execute(
                    "SELECT count(*) FROM chunks_fts WHERE user_id='u_demo'").fetchone()[0]
                self.assertEqual(fts_left, 0, "删除后 chunks_fts 不得残留用户正文")
                self.assertGreaterEqual(counts.get("chunks_fts", 0), 1, "counts 须记 chunks_fts 删除数")
            except sqlite3.OperationalError:
                pass  # FTS5 不可用 (信创降级) → 跳过
            # delete 审计须留存 (合规凭证, 不在被删范围)
            audit = conn.execute(
                "SELECT action FROM audit_log WHERE user_id='u_demo'").fetchall()
            actions = [r["action"] for r in audit]
            self.assertEqual(actions, ["delete"], "删除后仅留一条 delete 审计")
        finally:
            conn.close()

    def test_delete_with_message_linked_model_call(self) -> None:
        """DAL 层回归: model_calls.message_id REFERENCES messages。delete_user_data 须先删
        model_calls 再删 messages (见 mozi_backend/db/dal.py:370-371), 否则 foreign_keys=ON 下 FK 违约。
        本用例锁定该删除顺序: 携带非空 message_id 的 model_call 也能被清空 (与 HTTP 级回归互补)。
        """
        import sqlite3
        conn = fresh_conn()
        try:
            dal.ensure_user(conn, "u_demo", "u@mozi.local")
            sid = dal.create_session(conn, "u_demo", "会话", "auto")
            mid = dal.add_message(conn, sid, "user", "墨子是什么？")
            # 关键: model_call 携带非空 message_id (chat 真实路径如此)
            dal.record_model_call(conn, user_id="u_demo", message_id=mid, provider="glm",
                                  model="glm-5.2", tokens_in=1, tokens_out=1, cost_cny=0.0,
                                  latency_ms=1, strategy="balanced", fallback_used=False)
            # 期望: 删除成功且全空 (删除顺序已修复, model_calls 先于 messages)
            try:
                with transaction(conn):
                    dal.delete_user_data(conn, "u_demo")
            except sqlite3.IntegrityError as e:  # noqa: F841 — 缺陷现形, 使本用例失败
                self.fail(f"delete_user_data 触发 FK 约束 (删除顺序 bug): {e}")
            after = dal.export_user_data(conn, "u_demo")
            self.assertEqual(after["messages"], [], "删除后 messages 须为空")
            self.assertEqual(after["model_calls"], [], "删除后 model_calls 须为空")
        finally:
            conn.close()

    def test_delete_does_not_touch_other_user(self) -> None:
        conn = fresh_conn()
        try:
            self._seed_user(conn, "alice")
            self._seed_user(conn, "bob")
            with transaction(conn):
                dal.delete_user_data(conn, "alice")
            bob = dal.export_user_data(conn, "bob")
            self.assertEqual(len(bob["sessions"]), 1, "删 alice 不得影响 bob")
            self.assertTrue(bob["vault_documents"])
            alice = dal.export_user_data(conn, "alice")
            self.assertEqual(alice["sessions"], [])
        finally:
            conn.close()


class ExportDeleteApiTest(unittest.TestCase):
    """经 HTTP 端点 (multiuser) 验证 export/delete 端到端 + 身份注入。"""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from mozi_backend.main import app
        # 开 multiuser 让 X-User-Id 生效
        self._ctx = _SettingsCtx(require_auth=False, multiuser=True)
        self._new = self._ctx.__enter__()
        # gateway.api / sovereign 等模块在请求时读 auth.current_user_id, 已被 patch
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._ctx.__exit__()

    def test_export_via_api_and_isolation(self) -> None:
        h = _auth_headers("carol")
        # 建数据: chat (走 mock) 产生 session/message/vault/usage
        r = self.client.post("/v1/chat", headers=h, json={
            "messages": [{"role": "user", "content": "墨子用什么数据库？"}],
            "inject_context": False})
        self.assertEqual(r.status_code, 200)

        exp = self.client.get("/v1/export", headers=h).json()
        self.assertEqual(exp["user_id"], "carol")
        self.assertTrue(exp["data"]["sessions"], "export 含 carol 的 session")
        self.assertTrue(exp["data"]["vault_documents"], "export 含归档文档")

        # 另一个用户导出须为空 (隔离)
        other = self.client.get("/v1/export", headers=_auth_headers("dave")).json()
        self.assertEqual(other["data"]["sessions"], [], "dave 导出不含 carol 数据")

    def test_delete_after_chat(self) -> None:
        """HTTP 端到端回归: chat 产生的 model_calls.message_id FK→messages。
        DELETE /v1/account 须 200 且删后为空 (删除顺序已在 mozi_backend/db/dal.py:370-371 修复)。
        守护被遗忘权不回归。
        """
        h = _auth_headers("erin")
        self.client.post("/v1/chat", headers=h, json={
            "messages": [{"role": "user", "content": "墨子用什么数据库？"}],
            "inject_context": False})
        d = self.client.delete("/v1/account", headers=h)
        # 期望删除成功 + 删除后为空 (FK 删除顺序已修复)
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json()["status"], "deleted")
        after = self.client.get("/v1/export", headers=h).json()
        self.assertEqual(after["data"]["sessions"], [], "删除后 sessions 须为空")


class EventsAuthTest(unittest.TestCase):
    """/v1/events 鉴权 + 多用户隔离回归。

    缺陷: 端点曾缺 current_user_id 依赖 → require_auth 下无 token 仍 200;
          且 events.recent() 不按 user_id 过滤 → 一个用户看到全体遥测 (含 user_id/props
          会话/Skill 元数据)。本用例锚定两点: (a) 无 token 须 401; (b) 跨用户须隔离。
    """

    def test_events_requires_auth_under_require_auth(self) -> None:
        from fastapi.testclient import TestClient
        from mozi_backend.main import app
        with _SettingsCtx(require_auth=True, multiuser=False):
            with TestClient(app) as client:  # lifespan 生成/复用 device_token
                r = client.get("/v1/events")
                self.assertEqual(r.status_code, 401, "require_auth 下无 token 须 401")
                token = auth.get_device_token()
                ok = client.get("/v1/events", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(ok.status_code, 200, "正确 device_token 须放行")

    def test_events_isolated_per_user_under_multiuser(self) -> None:
        from fastapi.testclient import TestClient
        from mozi_backend.main import app
        from mozi_backend.telemetry import events
        with _SettingsCtx(require_auth=False, multiuser=True):
            with TestClient(app) as client:
                events.capture("chat_send", {"session_id": "s-a"}, user_id="evt_alice")
                events.capture("chat_send", {"session_id": "s-b"}, user_id="evt_bob")
                seen = client.get("/v1/events?limit=200",
                                  headers=_auth_headers("evt_alice")).json()["events"]
                self.assertTrue(any(e["user_id"] == "evt_alice" for e in seen),
                                "alice 须看到自己的事件")
                self.assertFalse(any(e["user_id"] == "evt_bob" for e in seen),
                                 "alice 不得看到 bob 的事件 (#7 隔离)")


class SkillEventsUserAttributionTest(unittest.TestCase):
    """#7 回归: skill 遥测须归属调用者 (multiuser), 不得落 default_user。

    缺陷: skills/api.py 的 events.capture(skill_loaded / skill_error[model]) 漏传 user_id,
    默认落 settings.default_user_id。GET /v1/events 现按 user_id 过滤后, 调用者看不到
    自己的 skill 遥测, 且全员 skill 活动混入 default_user。本用例锚定两点:
    (a) skill_invoked 须归属调用者; (b) 模型降级 skill_error 须归属调用者, 均不落 default_user。
    """

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from mozi_backend.main import app
        self._ctx = _SettingsCtx(require_auth=False, multiuser=True)
        self._ctx.__enter__()
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._ctx.__exit__()

    def _register_skill(self, **kw) -> int:
        from mozi_backend.db.database import get_conn
        params = dict(name="attr-skill", source="mozi", origin_path="/nonexistent/SKILL.md",
                      version="1", tier="A", capability={}, allowed_tools=[],
                      auto_invoke=True, scan_status="ok")
        params.update(kw)
        with get_conn() as conn:
            return dal.upsert_skill(conn, **params)

    def _seed_user(self, uid: str) -> None:
        from mozi_backend.db.database import get_conn
        with get_conn() as conn:
            dal.ensure_user(conn, uid, f"{uid}@mozi.local")

    def _events_for(self, uid: str) -> list[dict]:
        return self.client.get("/v1/events?limit=500",
                               headers=_auth_headers(uid)).json()["events"]

    def test_skill_invoked_attributed_to_caller(self) -> None:
        sid = self._register_skill(name="attr-invoked")
        self._seed_user("frank")
        inv = self.client.post("/v1/skills/invoke", headers=_auth_headers("frank"),
                               json={"skill_id": sid, "input": "墨子用什么数据库？"}).json()
        self.assertEqual(inv["status"], "ok")
        mine = [e for e in self._events_for("frank")
                if e["event"] == "skill_invoked" and e["props"].get("skill_id") == sid]
        self.assertTrue(mine, "frank 须看到自己的 skill_invoked")
        self.assertTrue(all(e["user_id"] == "frank" for e in mine))
        leaked = [e for e in self._events_for(auth.settings.default_user_id)
                  if e["event"] == "skill_invoked" and e["props"].get("skill_id") == sid]
        self.assertFalse(leaked, "skill_invoked 不得落 default_user")

    def test_skill_error_model_attributed_to_caller(self) -> None:
        import mozi_backend.skills.api as skapi
        sid = self._register_skill(name="attr-error")
        self._seed_user("grace")

        class _BoomAdapter:
            async def astream(self, spec, convo, usage):  # noqa: ANN001
                raise RuntimeError("boom")
                yield  # pragma: no cover — 使其为 async generator

        orig = skapi.select_adapter
        skapi.select_adapter = lambda spec: (_BoomAdapter(), False)
        try:
            inv = self.client.post("/v1/skills/invoke", headers=_auth_headers("grace"),
                                   json={"skill_id": sid, "input": "x"}).json()
        finally:
            skapi.select_adapter = orig
        self.assertEqual(inv["status"], "error", "astream 抛错须走降级分支")
        mine = [e for e in self._events_for("grace")
                if e["event"] == "skill_error" and e["props"].get("type") == "model"
                and e["props"].get("skill_id") == sid]
        self.assertTrue(mine, "grace 须看到自己的 skill_error(model)")
        self.assertTrue(all(e["user_id"] == "grace" for e in mine))
        leaked = [e for e in self._events_for(auth.settings.default_user_id)
                  if e["event"] == "skill_error" and e["props"].get("type") == "model"
                  and e["props"].get("skill_id") == sid]
        self.assertFalse(leaked, "skill_error(model) 不得落 default_user")


class SkillFreshUserEnsureUserTest(unittest.TestCase):
    """#7 回归: multiuser 下, 全新 X-User-Id 首请求即 /v1/skills/invoke 须端到端成功。

    缺陷: skills/api.py 的 invoke_skill 不像 gateway/api.py(chat/sessions) 与
    vault/api.py(archive) 那样先 dal.ensure_user。skill_calls.user_id /
    vault_documents.user_id 均 REFERENCES users(user_id) 且 PRAGMA foreign_keys=ON,
    故从未 chat/archive 过的全新用户首次 invoke 会在 record_skill_call /
    archive_document 处触发 FOREIGN KEY constraint failed → 500。
    本用例用一个从未播种的 uid 直接 invoke, 锚定: 不 500, status=ok, 且产物/计费已落库。
    """

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from mozi_backend.main import app
        self._ctx = _SettingsCtx(require_auth=False, multiuser=True)
        self._ctx.__enter__()
        # raise_server_exceptions=False: 让 500 以响应码呈现, 便于断言"不 500"
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._ctx.__exit__()

    def test_fresh_user_can_invoke_without_prior_chat(self) -> None:
        from mozi_backend.db.database import get_conn
        with get_conn() as conn:
            sid = dal.upsert_skill(conn, name="fresh-invoke", source="mozi",
                                   origin_path="/nonexistent/SKILL.md", version="1", tier="A",
                                   capability={}, allowed_tools=[], auto_invoke=True,
                                   scan_status="ok")
        uid = "newcomer_never_chatted"
        # 前置确认: 该用户在库中尚不存在 (从未 chat/archive)
        with get_conn() as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone(),
                "前置: 全新用户不得已存在")

        resp = self.client.post("/v1/skills/invoke", headers=_auth_headers(uid),
                                json={"skill_id": sid, "input": "墨子用什么数据库？"})
        self.assertEqual(resp.status_code, 200,
                         f"全新用户首次 invoke 不得 500 (FK 约束失败); 实际 {resp.status_code}")
        body = resp.json()
        self.assertEqual(body.get("status"), "ok", "降级 mock 适配器下应 status=ok")

        # invoke 须已隐式建用户, 且产物归档 + 调用计费均落在该用户名下
        with get_conn() as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone(),
                "invoke 须经 ensure_user 隐式建用户")
            self.assertTrue(conn.execute(
                "SELECT 1 FROM skill_calls WHERE user_id=? AND skill_id=?",
                (uid, sid)).fetchone(), "skill_calls 须归该用户")
            self.assertTrue(conn.execute(
                "SELECT 1 FROM vault_documents WHERE user_id=? AND type='skill'",
                (uid,)).fetchone(), "产物归档须归该用户")


class TokenProviderTest(unittest.TestCase):
    """v3 深化: 可插拔 provider / 过期 / 三模式分流 / 旧 v1 兼容 / device_token 0600。"""

    def test_hmac_token_expires(self) -> None:
        prov = auth.HmacV2Provider()
        expired = prov.sign("u", "sec", -10)             # exp 在过去
        self.assertIsNone(prov.verify(expired, "sec"))
        self.assertEqual(prov.verify(prov.sign("u", "sec", 3600), "sec"), "u")

    def test_token_roundtrip_both_providers(self) -> None:
        hp = auth.HmacV2Provider()
        self.assertEqual(hp.verify(hp.sign("u", "sec", 3600), "sec"), "u")
        try:
            import jwt
        except ImportError:
            self.skipTest("PyJWT 未装, 仅 HMAC-v2 路")
        jp = auth.JwtHs256Provider(jwt)
        self.assertEqual(jp.verify(jp.sign("u", "sec", 3600), "sec"), "u")

    def test_multiuser_strict_rejects_bare_xuid(self) -> None:
        with _SettingsCtx(multiuser=True, multiuser_allow_unsigned=False):
            with self.assertRaises(HTTPException) as ctx:
                auth.current_user_id(x_user_id="x", authorization=None)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_multiuser_allow_unsigned_transition(self) -> None:
        with _SettingsCtx(multiuser=True, multiuser_allow_unsigned=True):
            self.assertEqual(
                auth.current_user_id(x_user_id="trans", authorization=None), "trans")

    def test_multiuser_xuid_mismatch_403(self) -> None:
        with _SettingsCtx(multiuser=True):
            auth._DEVICE_TOKEN = "s"
            try:
                tok = auth.sign_user_token("alice")
                with self.assertRaises(HTTPException) as ctx:
                    auth.current_user_id(x_user_id="bob", authorization=f"Bearer {tok}")
                self.assertEqual(ctx.exception.status_code, 403)
            finally:
                auth._DEVICE_TOKEN = None

    def test_legacy_v1_token_still_verifies(self) -> None:
        import hashlib
        import hmac as _hmac
        sec = "legsec"
        body = "mozi.user.v1|legacy_user"
        mac = _hmac.new(sec.encode(), body.encode(), hashlib.sha256).digest()
        tok = f"{body}.{auth._b64u(mac)}"
        with _SettingsCtx(allow_legacy_v1=True):
            self.assertEqual(auth.verify_user_token(tok, sec), "legacy_user")
        with _SettingsCtx(allow_legacy_v1=False):
            self.assertIsNone(auth.verify_user_token(tok, sec))

    @unittest.skipUnless(os.name == "posix", "POSIX 文件权限")
    def test_device_token_file_mode_0600(self) -> None:
        import tempfile
        from pathlib import Path
        p = Path(tempfile.mkdtemp()) / ".device_token"
        with _SettingsCtx(device_token_path=p):
            auth._DEVICE_TOKEN = None
            try:
                auth.generate_device_token()
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            finally:
                auth._DEVICE_TOKEN = None


if __name__ == "__main__":
    unittest.main()
