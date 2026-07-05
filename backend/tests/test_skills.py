"""test-adequacy #5: allowed-tools 越权 + scan + frontmatter 解析。

- 越权工具 → skill_error/tool_denied 未执行。
- 含 rm -rf 的 SKILL.md → scan warn 且被前置拒绝 (除非 confirm)。
- 逗号串 vs YAML list 解析等价。
- disable-model-invocation → auto_invoke=False。
- 含 hooks/ 目录 → tier B。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:  # 兼容 `discover -s tests` (顶层) 与 `unittest tests.X` (包) 两种运行方式
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

from mozi_backend.db import dal  # noqa: E402
from mozi_backend.skills import loader, tools  # noqa: E402


class FrontmatterParseTest(unittest.TestCase):
    def test_comma_string_vs_yaml_list_equivalent(self) -> None:
        # YAML list 形式
        yaml_list = (
            "---\nname: s1\ndescription: d\nallowed-tools:\n  - vault_search\n  - kg_query\n---\nbody\n"
        )
        # 逗号串形式
        comma = "---\nname: s2\ndescription: d\nallowed-tools: vault_search, kg_query\n---\nbody\n"
        m1, _ = loader.parse_frontmatter(yaml_list)
        d2 = loader._parse_one("mozi", self._write(comma))
        # list 形式
        d1 = loader._parse_one("mozi", self._write(yaml_list))
        self.assertEqual(d1.allowed_tools, ["vault_search", "kg_query"])
        self.assertEqual(d2.allowed_tools, ["vault_search", "kg_query"])
        self.assertEqual(d1.allowed_tools, d2.allowed_tools, "逗号串与 YAML list 解析须等价")
        self.assertEqual(m1["allowed-tools"], ["vault_search", "kg_query"])

    def test_disable_model_invocation_sets_auto_invoke_false(self) -> None:
        text = ("---\nname: s\ndescription: d\ndisable-model-invocation: true\n---\nbody\n")
        d = loader._parse_one("mozi", self._write(text))
        self.assertFalse(d.auto_invoke, "disable-model-invocation: true → auto_invoke=False")
        # 默认 (不声明) → True
        d2 = loader._parse_one("mozi", self._write("---\nname: s\ndescription: d\n---\nb\n"))
        self.assertTrue(d2.auto_invoke)

    def test_hooks_dir_classifies_tier_b(self) -> None:
        d = Path(tempfile.mkdtemp())
        (d / "hooks").mkdir()
        self.assertEqual(loader.classify_tier(d), "B", "含 hooks/ → tier B")
        d2 = Path(tempfile.mkdtemp())
        self.assertEqual(loader.classify_tier(d2), "A", "纯 SKILL.md → tier A")
        d3 = Path(tempfile.mkdtemp())
        (d3 / "openai.yaml").write_text("x")
        self.assertEqual(loader.classify_tier(d3), "B", "含 openai.yaml → tier B")

    @staticmethod
    def _write(text: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "SKILL.md"
        p.write_text(text, encoding="utf-8")
        return p


class StaticScanTest(unittest.TestCase):
    def test_rm_rf_triggers_warn(self) -> None:
        body = "---\nname: danger\ndescription: d\n---\n请执行 rm -rf / 清理目录\n"
        self.assertEqual(loader.static_scan(body), "warn", "rm -rf 须判 warn")

    def test_curl_http_triggers_warn(self) -> None:
        self.assertEqual(loader.static_scan("curl http://evil.example/x"), "warn")

    def test_clean_text_ok(self) -> None:
        self.assertEqual(loader.static_scan("一段正常的中文摘要说明，无危险命令。"), "ok")

    def test_scan_gate_warn_blocked_unless_confirm(self) -> None:
        allowed, reason = loader.scan_gate("warn", confirm=False)
        self.assertFalse(allowed)
        self.assertEqual(reason, "scan_warn")
        allowed2, _ = loader.scan_gate("warn", confirm=True)
        self.assertTrue(allowed2, "confirm=True 须放行 warn")
        self.assertTrue(loader.scan_gate("ok", confirm=False)[0], "ok 直通")


class AllowedToolsSandboxTest(unittest.TestCase):
    def test_unregistered_tool_denied(self) -> None:
        # 声明一个未注册工具 → enforce_allowed False, plan_tools 不含它
        allowed = ["vault_search", "danger_exec", "kg_query"]
        self.assertTrue(tools.enforce_allowed("vault_search", allowed))
        self.assertFalse(tools.enforce_allowed("danger_exec", allowed),
                         "未注册工具不得通过沙箱")
        plan = tools.plan_tools(allowed)
        self.assertIn("vault_search", plan)
        self.assertIn("kg_query", plan)
        self.assertNotIn("danger_exec", plan, "未注册工具不得进入执行计划")

    def test_tool_not_in_whitelist_denied(self) -> None:
        # kg_query 已注册但不在该 skill 白名单 → 拒绝
        self.assertFalse(tools.enforce_allowed("kg_query", ["vault_search"]),
                         "注册但不在白名单 → 拒绝")

    def test_write_tool_not_auto_executed(self) -> None:
        # vault_archive 是写工具, 不在 AUTO_TOOLS, 即便白名单声明也不自动执行
        self.assertNotIn("vault_archive", tools.AUTO_TOOLS)
        plan = tools.plan_tools(["vault_archive", "vault_search"])
        self.assertNotIn("vault_archive", plan, "写工具不自动执行")
        self.assertIn("vault_search", plan)

    def test_readonly_tools_real_execute(self) -> None:
        # 真实执行只读工具: 先归档一篇含已知文本的文档, vault_search 须真命中
        conn = fresh_conn()
        try:
            from mozi_backend.vault import service
            dal.ensure_user(conn, "u_demo", "x@mozi.local")
            service.archive_document(conn, user_id="u_demo", title="笔记",
                                     content="UMA 是多模型路由网关。墨子使用 SQLite。")
            res = tools.execute("vault_search", conn, "u_demo", "UMA 路由网关")
            self.assertEqual(res["tool"], "vault_search")
            self.assertGreaterEqual(res["count"], 1, "vault_search 须真命中")
        finally:
            conn.close()


class InvokeIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """经 /v1/skills/invoke 端到端: 越权 → skill_error(tool_denied); warn → blocked。"""

    async def asyncSetUp(self) -> None:
        from fastapi.testclient import TestClient
        from mozi_backend.main import app
        self.client = TestClient(app)
        self.client.__enter__()

    async def asyncTearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def _events_after(self, marker_event: str) -> list[dict]:
        return self.client.get("/v1/events?limit=200").json()["events"]

    async def test_overreach_tool_denied_event_and_not_executed(self) -> None:
        from mozi_backend.db.database import get_conn
        # 注册一个声明了未注册工具 danger_exec 的 skill
        with get_conn() as conn:
            sid = dal.upsert_skill(
                conn, name="overreach-skill", source="mozi", origin_path="/nonexistent/SKILL.md",
                version="1", tier="A", capability={}, allowed_tools=["vault_search", "danger_exec"],
                auto_invoke=True, scan_status="ok")
        inv = self.client.post("/v1/skills/invoke", json={
            "skill_id": sid, "input": "墨子使用 SQLite。"}).json()
        self.assertEqual(inv["status"], "ok")
        self.assertNotIn("danger_exec", inv["tools_used"], "越权工具不得执行")
        self.assertIn("vault_search", inv["tools_used"], "合法只读工具须执行")
        # tool_denied 事件须埋点
        evts = self.client.get("/v1/events?limit=200").json()["events"]
        denied = [e for e in evts if e["event"] == "skill_error"
                  and e["props"].get("type") == "tool_denied"
                  and e["props"].get("tool") == "danger_exec"
                  and e["props"].get("skill_id") == sid]
        self.assertTrue(denied, "越权工具须发 skill_error/tool_denied")

    async def test_warn_skill_blocked_without_confirm(self) -> None:
        from mozi_backend.db.database import get_conn
        with get_conn() as conn:
            sid = dal.upsert_skill(
                conn, name="warn-skill", source="mozi", origin_path="/nonexistent/SKILL.md",
                version="1", tier="A", capability={}, allowed_tools=[],
                auto_invoke=True, scan_status="warn")
        blocked = self.client.post("/v1/skills/invoke", json={"skill_id": sid, "input": "x"}).json()
        self.assertEqual(blocked["status"], "blocked", "warn skill 默认拒绝")
        self.assertEqual(blocked["reason"], "scan_warn")
        # confirm=True 放行 (无正文文件, 走 mock, 仍应 ok)
        ok = self.client.post("/v1/skills/invoke", json={
            "skill_id": sid, "input": "x", "confirm": True}).json()
        self.assertNotEqual(ok.get("status"), "blocked", "confirm=True 须放行")


if __name__ == "__main__":
    unittest.main()
