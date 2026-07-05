"""EPIC-METER: 中央出网 choke 守卫 (AST 源码反射 + classify/audit 语义)。

架构红线: egress=True 字面只准出现在 gateway/egress.py; 受控文件挪门即红灯。
AST (非脆弱 regex): 跳过注释/docstring/字符串, 只匹配 Call 的 egress=True 关键字实参。
"""
from __future__ import annotations

import ast
import pathlib
import unittest

try:
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

import mozi_backend  # noqa: E402
from mozi_backend.gateway import egress  # noqa: E402

_PKG = pathlib.Path(mozi_backend.__file__).resolve().parent
GUARDED = ["gateway/orchestrator.py", "skills/api.py", "skills/tools.py", "gateway/api.py"]
OWNER = "gateway/egress.py"


def _egress_true_keyword_hits(path: pathlib.Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "egress" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    hits += 1
    return hits


class ChokePointTest(unittest.TestCase):
    def test_guarded_files_have_no_egress_true_literal(self) -> None:
        for rel in GUARDED:
            self.assertEqual(_egress_true_keyword_hits(_PKG / rel), 0,
                             f"{rel} 不得直接写 egress=True (须经 egress.audit 唯一门)")

    def test_egress_module_owns_the_literal(self) -> None:
        self.assertGreaterEqual(_egress_true_keyword_hits(_PKG / OWNER), 1,
                                "egress.py 须是唯一持有 egress=True 的门")

    def test_egress_actions_whitelist(self) -> None:
        self.assertEqual(egress.EGRESS_ACTIONS,
                         {"model.infer", "skill.infer", "tool.web_search", "telemetry.report"})


class ClassifyAuditTest(unittest.TestCase):
    def test_classify_local_no_egress(self) -> None:
        self.assertFalse(egress.classify(provider="local", privacy_tier="cloud", is_real=True).egress)
        self.assertFalse(egress.classify(provider="glm", privacy_tier="cloud", is_real=False).egress)

    def test_classify_sovereign_blocks_foreign(self) -> None:
        v = egress.classify(provider="anthropic", privacy_tier="sovereign", is_real=True)
        self.assertFalse(v.allowed)
        self.assertTrue(v.egress)

    def test_audit_real_writes_one_row(self) -> None:
        conn = fresh_conn()
        try:
            dal_user(conn, "u")
            egress.audit(conn, user_id="u", provider="glm", action="model.infer",
                         resource="glm-5.2", privacy_tier="local_first", is_real=True)
            rows = conn.execute(
                "SELECT egress_flag FROM audit_log WHERE action='model.infer'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], 1)
        finally:
            conn.close()

    def test_audit_mock_writes_no_infer_row(self) -> None:
        conn = fresh_conn()
        try:
            dal_user(conn, "u")
            egress.audit(conn, user_id="u", provider="local", action="model.infer",
                         resource="llama-local", is_real=False)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM audit_log WHERE action='model.infer'").fetchone()[0], 0,
                "mock/local 不写 model.infer 行 (BLOCKER 核心语义)")
        finally:
            conn.close()

    def test_web_search_audited(self) -> None:
        conn = fresh_conn()
        try:
            dal_user(conn, "u")
            v = egress.audit(conn, user_id="u", provider="web_search", action="tool.web_search",
                             resource="热点新闻", is_real=True)
            self.assertTrue(v.egress)
            self.assertEqual(conn.execute(
                "SELECT egress_flag FROM audit_log WHERE action='tool.web_search'").fetchone()[0], 1)
        finally:
            conn.close()


def dal_user(conn, uid: str) -> None:
    from mozi_backend.db import dal
    dal.ensure_user(conn, uid, f"{uid}@x.cn")


if __name__ == "__main__":
    unittest.main()
