"""EPIC-SKILL: tier C 落地 + scan_gate tier + 工具注册表 + invoke 溯源 + /tools + read_reference。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

try:
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

from mozi_backend.main import app  # noqa: E402
from mozi_backend.skills import loader, tools  # noqa: E402
from mozi_backend.skills.tools import ToolSpec  # noqa: E402


def _skill_dir(**files) -> Path:
    d = Path(tempfile.mkdtemp(prefix="mozi_skill_"))
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


class TierClassifyTest(unittest.TestCase):
    def test_scan_warn_yields_c(self) -> None:
        self.assertEqual(loader.classify_tier(_skill_dir(), scan_status="warn"), "C")

    def test_overreach_tool_yields_c(self) -> None:
        self.assertEqual(loader.classify_tier(_skill_dir(), allowed_tools=["danger_exec"]), "C")

    def test_clean_is_a(self) -> None:
        self.assertEqual(loader.classify_tier(_skill_dir(), allowed_tools=["vault_search"]), "A")

    def test_hooks_is_b(self) -> None:
        d = _skill_dir(**{"hooks/x.sh": "echo hi"})
        self.assertEqual(loader.classify_tier(d), "B")

    def test_c_overrides_b(self) -> None:
        d = _skill_dir(**{"hooks/x.sh": "echo hi"})
        self.assertEqual(loader.classify_tier(d, scan_status="warn"), "C", "scan warn 压过 hooks B")

    def test_single_arg_backward_compat(self) -> None:
        self.assertEqual(loader.classify_tier(_skill_dir()), "A")  # 旧单参调用仍工作


class ScanGateTierTest(unittest.TestCase):
    def test_tier_c_blocked_unless_confirm(self) -> None:
        self.assertEqual(loader.scan_gate("ok", False, tier="C"), (False, "tier_c"))
        self.assertTrue(loader.scan_gate("ok", True, tier="C")[0])

    def test_tier_none_backward_compat(self) -> None:
        self.assertEqual(loader.scan_gate("warn", False), (False, "scan_warn"))
        self.assertTrue(loader.scan_gate("ok", False)[0])


class StaticScanDegradeTest(unittest.TestCase):
    def test_regex_degrade_path(self) -> None:
        self.assertEqual(loader.static_scan("rm -rf /"), "warn")
        self.assertEqual(loader.static_scan("一段正常中文说明"), "ok")


class ToolRegistryTest(unittest.TestCase):
    def test_register_unregister(self) -> None:
        spec = ToolSpec("unit_probe", lambda *a, **k: {}, True, "测试工具")
        tools.register(spec)
        self.addCleanup(tools.unregister, "unit_probe")
        self.assertTrue(tools.is_registered("unit_probe"))
        self.assertIn("unit_probe", tools.AUTO_TOOLS)
        with self.assertRaises(ValueError):
            tools.register(spec)                       # 重名非 override
        tools.unregister("unit_probe")
        self.assertFalse(tools.is_registered("unit_probe"))
        self.assertNotIn("unit_probe", tools.AUTO_TOOLS)


class ReadReferenceTest(unittest.TestCase):
    def test_path_traversal_denied(self) -> None:
        d = _skill_dir(**{"references/x.md": "正文内容"})
        self.assertIsNone(loader.read_reference(d, "../../../etc/passwd"))
        self.assertEqual(loader.read_reference(d, "references/x.md"), "正文内容")


class InvokeTraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.__enter__()
        self.client.post("/v1/skills/discover")
        self.sid = next(s for s in self.client.get("/v1/skills").json()["skills"]
                        if s["name"] == "mozi-demo")["skill_id"]

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def _last_call(self):
        from mozi_backend.db.database import get_conn
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM skill_calls WHERE skill_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (self.sid,)).fetchone()

    def test_trace_fields_and_archived(self) -> None:
        r = self.client.post("/v1/skills/invoke", json={"skill_id": self.sid, "input": "墨子用 SQLite。"})
        self.assertEqual(r.json()["status"], "ok")
        call = self._last_call()
        self.assertIsInstance(call["latency_ms"], int)
        self.assertIsNotNone(call["archived_doc_id"], "archived_doc_id 须非空")
        from mozi_backend.db.database import get_conn
        with get_conn() as conn:
            self.assertTrue(conn.execute("SELECT 1 FROM vault_documents WHERE doc_id=?",
                                         (call["archived_doc_id"],)).fetchone())

    def test_message_id_when_session(self) -> None:
        sid = self.client.post("/v1/sessions", json={"title": "t"}).json()["session_id"]
        self.client.post("/v1/skills/invoke",
                         json={"skill_id": self.sid, "input": "x", "session_id": sid})
        self.assertIsNotNone(self._last_call()["message_id"], "带 session → message_id 非空")

    def test_invalid_session_no_500(self) -> None:
        r = self.client.post("/v1/skills/invoke",
                             json={"skill_id": self.sid, "input": "x", "session_id": "ghost_sid"})
        self.assertEqual(r.status_code, 200, "不存在 session 不得 500 (session_ok 守卫)")
        self.assertIsNone(self._last_call()["message_id"], "不存在 session → message_id None")


class ListToolsApiTest(unittest.TestCase):
    def test_list_tools_endpoint(self) -> None:
        with TestClient(app) as client:
            tl = {t["name"]: t for t in client.get("/v1/skills/tools").json()["tools"]}
        self.assertTrue(tl["vault_search"]["readonly"])
        self.assertTrue(tl["vault_search"]["auto"])
        self.assertFalse(tl["vault_archive"]["readonly"])


class SampleSkillTierTest(unittest.TestCase):
    def test_mozi_demo_is_tier_a(self) -> None:
        """BLOCKER 回归: mozi-demo allowed-tools 改注册名后须判 tier A (无 confirm 可 invoke)。"""
        conn = fresh_conn()
        try:
            descs = loader.discover()
            demo = next(d for d in descs if d.name == "mozi-demo")
            self.assertEqual(demo.tier, "A", "mozi-demo 须 tier A (vault_search/kg_query 均注册)")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
