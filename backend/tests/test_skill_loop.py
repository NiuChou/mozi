"""PR3 (P0-A 阶段3): skill invoke 接有界工具循环 (门控 MOZI_AGENT_LOOP)。

零外呼: mock 适配器 + input 埋 [[mock-tool:NAME]] 触发确定性工具调用。
"""
from __future__ import annotations

import os
import unittest

try:  # 兼容两种运行方式
    from ._helpers import fresh_conn  # noqa: F401  (确保 _helpers 副作用: 临时库 + 环境)
except ImportError:
    from _helpers import fresh_conn  # noqa: F401

from mozi_backend.db import dal  # noqa: E402
from mozi_backend.db.database import get_conn, init_db  # noqa: E402
from mozi_backend.gateway import agent_loop  # noqa: E402
from mozi_backend.gateway.adapters.base import ToolCall, ToolCallsReady  # noqa: E402
from mozi_backend.schemas import SkillInvokeRequest  # noqa: E402
from mozi_backend.skills import api as skapi  # noqa: E402
from mozi_backend.skills.api import invoke_skill  # noqa: E402

USER = "u-skloop"
init_db(os.environ["MOZI_DB_PATH"])   # invoke 经 get_conn 用配置库 → 须先建表


class _RealToolStub:
    """is_real=True 桩: 首步真实 usage + 调 vault_search, 次步出文本。"""

    def __init__(self) -> None:
        self.n = 0

    async def astream(self, spec, messages, usage=None, *, tools=None, transport=None):
        self.n += 1
        if self.n == 1:
            if usage is not None:
                usage["tokens_in"], usage["tokens_out"] = 80, 15
            yield ToolCallsReady([ToolCall("r1", "vault_search", {"query": "x"})])
        else:
            if usage is not None:
                usage["tokens_in"], usage["tokens_out"] = 40, 8
            yield "结果"


class _BoomStub:
    async def astream(self, spec, messages, usage=None, *, tools=None, transport=None):
        raise RuntimeError("boom")
        yield ""  # pragma: no cover


def _patch_select(case, adapter, is_real: bool) -> None:
    """同时改 skills.api 与 agent_loop 两处 select_adapter 引用。"""
    for mod in (skapi, agent_loop):
        case.addCleanup(setattr, mod, "select_adapter", mod.select_adapter)
        mod.select_adapter = lambda spec, _a=adapter, _r=is_real: (_a, _r)


def _last_skill_call(skill_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM skill_calls WHERE skill_id=? ORDER BY created_at DESC LIMIT 1",
                           (skill_id,)).fetchone()
    return dict(row) if row else {}


def _seed_skill(allowed) -> str:
    with get_conn() as conn:
        dal.ensure_user(conn, USER, f"{USER}@mozi.local", "CN")
        return dal.upsert_skill(conn, name="loop-demo", source="mozi", origin_path="(none)",
                                version="1", tier="A", capability={}, allowed_tools=allowed,
                                auto_invoke=True, scan_status="ok")


def _set_loop(case, val: str | None) -> None:
    old = os.environ.get("MOZI_AGENT_LOOP")
    if val is None:
        os.environ.pop("MOZI_AGENT_LOOP", None)
    else:
        os.environ["MOZI_AGENT_LOOP"] = val
    case.addCleanup(lambda: os.environ.__setitem__("MOZI_AGENT_LOOP", old) if old is not None
                    else os.environ.pop("MOZI_AGENT_LOOP", None))


class SkillLoopWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_loop_on_runs_agentic_loop(self) -> None:
        _set_loop(self, "1")
        sid = _seed_skill(["vault_search"])
        res = await invoke_skill(
            SkillInvokeRequest(skill_id=sid, input="查 [[mock-tool:vault_search]] 墨子"), user_id=USER)
        self.assertEqual(res["status"], "ok")
        self.assertIn("run_id", res)
        self.assertTrue(res["run_id"])
        self.assertIn("vault_search", res["tools_used"])
        self.assertTrue(res["output"], "应有模型输出")
        self.assertGreaterEqual(res.get("steps", 0), 2, "工具步 + 终答步")
        with get_conn() as conn:
            rows = dal.list_agent_steps(conn, res["run_id"])
        self.assertGreaterEqual(len(rows), 2, "agent_steps 应持久化")

    async def test_loop_off_uses_legacy_static(self) -> None:
        _set_loop(self, "0")
        sid = _seed_skill(["vault_search"])
        res = await invoke_skill(
            SkillInvokeRequest(skill_id=sid, input="查 [[mock-tool:vault_search]] 墨子"), user_id=USER)
        self.assertEqual(res["status"], "ok")
        self.assertIsNone(res.get("run_id"), "旧静态路径不产 run_id")
        # 旧路径静态预取仍执行只读工具
        self.assertIn("vault_search", res["tools_used"])

    async def test_no_allowed_tools_skips_loop(self) -> None:
        # 无 allowed_tools → 即便开关开也不进循环 (退旧静态路径); 仍落 skill_calls 行
        _set_loop(self, "1")
        sid = _seed_skill([])
        res = await invoke_skill(
            SkillInvokeRequest(skill_id=sid, input="纯文本问题"), user_id=USER)
        self.assertEqual(res["status"], "ok")
        self.assertIsNone(res.get("run_id"))
        self.assertEqual(res["tools_used"], [])
        self.assertTrue(_last_skill_call(sid), "静态路径仍须落 skill_calls")

    async def test_loop_real_provider_persists_and_audits(self) -> None:
        # is_real=True 走循环: skill_calls 计费行 + model.infer 出网审计 + tokens 聚合
        _set_loop(self, "1")
        _patch_select(self, _RealToolStub(), is_real=True)
        sid = _seed_skill(["vault_search"])
        res = await invoke_skill(
            SkillInvokeRequest(skill_id=sid, input="查墨子"), user_id=USER)
        self.assertEqual(res["status"], "ok")
        self.assertIn("vault_search", res["tools_used"])
        self.assertTrue(res["metered"], "真实 usage 须计费")
        self.assertGreater(res["cost_cny"], 0.0)
        call = _last_skill_call(sid)
        self.assertEqual(call["egress_flag"], 1)
        self.assertGreater(call["tokens_in"], 0)
        self.assertTrue(call["archived_doc_id"], "产物须归档")
        self.assertIn("vault_search", call["tools_used"])
        with get_conn() as conn:
            row = conn.execute("SELECT egress_flag FROM audit_log WHERE user_id=? AND action='model.infer' "
                               "ORDER BY created_at DESC LIMIT 1", (USER,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1, "循环逐步审计 model.infer 出网")

    async def test_loop_sovereign_blocked_no_egress_flag(self) -> None:
        # sovereign + 非国产模型 (绕 route 强塞) → 循环入口出网被拒 → blocked, egress_flag=0
        _set_loop(self, "1")
        _patch_select(self, _RealToolStub(), is_real=True)
        self.addCleanup(setattr, skapi, "route", skapi.route)
        skapi.route = lambda req: type("D", (), {"chosen_model": "claude", "strategy": "manual"})()
        sid = _seed_skill(["vault_search"])
        res = await invoke_skill(
            SkillInvokeRequest(skill_id=sid, input="查"), user_id=USER)  # local_first=1 → privacy=sovereign
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(_last_skill_call(sid)["egress_flag"], 0, "零字节出网 → flag 不得为 1")

    async def test_static_model_failure_degrades(self) -> None:
        # 旧静态路径模型流抛错 → status=error, 降级文案
        _set_loop(self, "0")
        _patch_select(self, _BoomStub(), is_real=False)
        sid = _seed_skill(["vault_search"])
        res = await invoke_skill(
            SkillInvokeRequest(skill_id=sid, input="查 [[mock-tool:vault_search]]"), user_id=USER)
        self.assertEqual(res["status"], "error")
        self.assertTrue(res["output"].startswith("[skill 调用降级]"))
        self.assertEqual(_last_skill_call(sid)["status"], "error")


if __name__ == "__main__":
    unittest.main()
