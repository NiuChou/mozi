"""PR2 (P0-A 阶段2): 有界 agentic 工具循环引擎 + agent_steps 持久化。

零外呼: mock 适配器确定性驱动 (无 web_search 网络工具入用例)。
循环未接生产路径 (orchestrator/skill), 直接对 run_tool_loop 单测。
"""
from __future__ import annotations

import unittest

try:  # 兼容两种运行方式
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

from mozi_backend.db import dal  # noqa: E402
from mozi_backend.gateway import agent_loop  # noqa: E402
from mozi_backend.gateway.adapters.base import ToolCall, ToolCallsReady  # noqa: E402
from mozi_backend.gateway.models import get_model  # noqa: E402


async def run_loop(conn, **kw) -> list[dict]:
    """收集 run_tool_loop 的全部事件。"""
    out: list[dict] = []
    async for ev in agent_loop.run_tool_loop(conn, **kw):
        out.append(ev)
    return out


class _AlwaysToolAdapter:
    """测试桩: 每次都请求 vault_search (永不收尾), 用于驱动 max_steps / 配额护栏分支。"""

    async def astream(self, spec, messages, usage=None, *, tools=None, transport=None):
        yield ToolCallsReady([ToolCall("always-1", "vault_search", {"query": "x"})])


class _UsageToolAdapter:
    """桩: 首步写真实 usage + 请求工具, 次步写 usage + 出文本。验证 tokens 落库非 0。"""

    def __init__(self) -> None:
        self.calls = 0

    async def astream(self, spec, messages, usage=None, *, tools=None, transport=None):
        self.calls += 1
        if self.calls == 1:
            if usage is not None:
                usage["tokens_in"], usage["tokens_out"] = 100, 20
            yield ToolCallsReady([ToolCall("u1", "vault_search", {"query": "x"})])
        else:
            if usage is not None:
                usage["tokens_in"], usage["tokens_out"] = 50, 10
            yield "终答"


class _MultiCallAdapter:
    """桩: 首步同回合两调用 (一准一拒), 次步收尾。"""

    def __init__(self) -> None:
        self.calls = 0

    async def astream(self, spec, messages, usage=None, *, tools=None, transport=None):
        self.calls += 1
        if self.calls == 1:
            yield ToolCallsReady([ToolCall("c-a", "vault_search", {"query": "x"}),
                                  ToolCall("c-b", "vault_archive", {"query": "y"})])
        else:
            yield "汇总"


class _PureTextAdapter:
    """桩: 只出文本 delta, 永不请求工具 → 确定性 final (不依赖 mock 收敛)。"""

    async def astream(self, spec, messages, usage=None, *, tools=None, transport=None):
        for t in ("答", "案"):
            yield t


def _patch_adapter(case, adapter, is_real=False):
    case.addCleanup(setattr, agent_loop, "select_adapter", agent_loop.select_adapter)
    agent_loop.select_adapter = lambda spec: (adapter, is_real)


class SupportsToolsFlagTest(unittest.TestCase):
    def test_default_true_local_false(self) -> None:
        self.assertIs(get_model("glm-5.2").supports_tools, True)
        self.assertIs(get_model("deepseek-v4-pro").supports_tools, True)
        # 本地 llama 走 mock, 不具 function-calling → 退单发
        self.assertIs(get_model("llama-local").supports_tools, False)


class AgentStepDalTest(unittest.TestCase):
    def test_record_and_list_roundtrip(self) -> None:
        conn = fresh_conn()
        dal.ensure_user(conn, "u-step", "u-step@mozi.local", "CN")
        sid = dal.record_agent_step(conn, run_id="run-1", user_id="u-step", step_idx=0,
                                    tool="vault_search", tokens_in=10, tokens_out=5,
                                    latency_ms=42, egress=False, status="ok")
        self.assertTrue(sid)
        rows = dal.list_agent_steps(conn, "run-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "vault_search")
        self.assertEqual(rows[0]["step_idx"], 0)
        self.assertEqual(rows[0]["latency_ms"], 42)
        self.assertEqual(rows[0]["egress"], 0)

    def test_list_scoped_by_run_and_ordered(self) -> None:
        conn = fresh_conn()
        dal.ensure_user(conn, "u-step", "u-step@mozi.local", "CN")
        dal.record_agent_step(conn, run_id="run-A", user_id="u-step", step_idx=1, tool="b")
        dal.record_agent_step(conn, run_id="run-A", user_id="u-step", step_idx=0, tool="a")
        dal.record_agent_step(conn, run_id="run-B", user_id="u-step", step_idx=0, tool="x")
        rows = dal.list_agent_steps(conn, "run-A")
        self.assertEqual([r["step_idx"] for r in rows], [0, 1], "同 run 按 step_idx 升序")
        self.assertEqual([r["tool"] for r in rows], ["a", "b"])


class RunToolLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.conn = fresh_conn()
        dal.ensure_user(self.conn, "u-loop", "u-loop@mozi.local", "CN")
        self.spec = get_model("glm-5.2")   # 无 key → select_adapter 回退 mock

    async def test_mock_loop_end_to_end(self) -> None:
        convo = [{"role": "user", "content": "查知识库 [[mock-tool:vault_search]] 墨子"}]
        events = await run_loop(self.conn, user_id="u-loop", convo=convo,
                                allowed_tools=["vault_search"], spec=self.spec, run_id="r-e2e")
        types = [e["type"] for e in events]
        self.assertIn("tool_call", types)
        self.assertIn("tool_result", types)
        self.assertTrue(any(e["type"] == "delta" for e in events), "终答须流式文本")
        self.assertTrue(any(e["type"] == "step" and e.get("kind") == "final" for e in events))
        tc = next(e for e in events if e["type"] == "tool_call")
        self.assertEqual(tc["name"], "vault_search")
        steps = dal.list_agent_steps(self.conn, "r-e2e")
        self.assertGreaterEqual(len(steps), 2, "工具步 + 终答步")

    async def test_sandbox_denies_unallowed_tool(self) -> None:
        # 模型请求 vault_archive, 但白名单只含 vault_search → 拒执行
        convo = [{"role": "user", "content": "归档 [[mock-tool:vault_archive]] 这段"}]
        events = await run_loop(self.conn, user_id="u-loop", convo=convo,
                                allowed_tools=["vault_search"], spec=self.spec, run_id="r-deny")
        self.assertTrue(any(e["type"] == "tool_denied" and e["name"] == "vault_archive"
                            for e in events), "越权工具须 tool_denied")
        # vault_archive 未执行 → 无文档归档
        docs = self.conn.execute("SELECT count(*) FROM vault_documents WHERE user_id='u-loop'").fetchone()[0]
        self.assertEqual(docs, 0)
        self.assertTrue(any(e["type"] == "delta" for e in events), "拒后仍收尾出文本")

    async def test_max_steps_caps_loop(self) -> None:
        self.addCleanup(setattr, agent_loop, "select_adapter", agent_loop.select_adapter)
        agent_loop.select_adapter = lambda spec: (_AlwaysToolAdapter(), False)
        convo = [{"role": "user", "content": "循环"}]
        events = await run_loop(self.conn, user_id="u-loop", convo=convo,
                                allowed_tools=["vault_search"], spec=self.spec,
                                run_id="r-max", max_steps=3)
        limit = [e for e in events if e["type"] == "step_limit"]
        self.assertEqual(len(limit), 1)
        self.assertEqual(limit[0]["reason"], "max_steps")
        self.assertEqual(len(dal.list_agent_steps(self.conn, "r-max")), 3, "恰 max_steps 步")

    async def test_over_hard_cap_stops_between_steps(self) -> None:
        self.addCleanup(setattr, agent_loop, "select_adapter", agent_loop.select_adapter)
        agent_loop.select_adapter = lambda spec: (_AlwaysToolAdapter(), False)
        self.addCleanup(setattr, agent_loop.quota, "over_hard_cap", agent_loop.quota.over_hard_cap)
        agent_loop.quota.over_hard_cap = lambda conn, uid: True   # 步后即超硬上限
        convo = [{"role": "user", "content": "循环"}]
        events = await run_loop(self.conn, user_id="u-loop", convo=convo,
                                allowed_tools=["vault_search"], spec=self.spec,
                                run_id="r-cap", max_steps=5)
        limit = [e for e in events if e["type"] == "step_limit"]
        self.assertEqual(limit[0]["reason"], "quota")
        self.assertEqual(len(dal.list_agent_steps(self.conn, "r-cap")), 1, "首步后即止")

    async def test_unsupported_model_single_shot(self) -> None:
        # llama-local supports_tools=False → 即便埋 trigger 也不进循环, 纯文本
        spec = get_model("llama-local")
        convo = [{"role": "user", "content": "查 [[mock-tool:vault_search]]"}]
        events = await run_loop(self.conn, user_id="u-loop", convo=convo,
                                allowed_tools=["vault_search"], spec=spec, run_id="r-uns")
        self.assertEqual([e for e in events if e["type"] in ("tool_call", "tool_result")], [])
        self.assertTrue(any(e["type"] == "delta" for e in events))


class LoopHardeningTest(unittest.IsolatedAsyncioTestCase):
    """review 确认项回归: usage 落库 / convo 合法形状 / sovereign 拦截 / 多调用 / 错误 / 状态。"""

    def setUp(self) -> None:
        self.conn = fresh_conn()
        dal.ensure_user(self.conn, "u-h", "u-h@mozi.local", "CN")
        self.spec = get_model("glm-5.2")

    async def test_toolcall_step_records_real_usage(self) -> None:
        # #1: 工具步不得因 break 丢 usage 帧 → tokens 落库非 0
        _patch_adapter(self, _UsageToolAdapter(), is_real=True)
        await run_loop(self.conn, user_id="u-h", convo=[{"role": "user", "content": "q"}],
                       allowed_tools=["vault_search"], spec=self.spec, run_id="r-usage")
        steps = dal.list_agent_steps(self.conn, "r-usage")
        self.assertEqual(steps[0]["tokens_in"], 100, "工具步须捕获 usage, 非 0")
        self.assertEqual(steps[0]["tokens_out"], 20)

    async def test_convo_observation_is_openai_valid(self) -> None:
        # #2: 工具观测须为 OpenAI 合法形状 (assistant.tool_calls + tool.tool_call_id)
        convo = [{"role": "user", "content": "q"}]
        _patch_adapter(self, _UsageToolAdapter(), is_real=False)
        await run_loop(self.conn, user_id="u-h", convo=convo,
                       allowed_tools=["vault_search"], spec=self.spec, run_id="r-shape")
        asst = next(m for m in convo if m["role"] == "assistant" and "tool_calls" in m)
        self.assertEqual(asst["tool_calls"][0]["id"], "u1")
        self.assertEqual(asst["tool_calls"][0]["function"]["name"], "vault_search")
        import json as _json
        self.assertEqual(_json.loads(asst["tool_calls"][0]["function"]["arguments"]), {"query": "x"})
        toolmsg = next(m for m in convo if m["role"] == "tool")
        self.assertEqual(toolmsg["tool_call_id"], "u1", "tool 消息须带 tool_call_id")
        self.assertNotIn("tool_call", toolmsg, "不得用非法 tool_call 键")

    async def test_sovereign_blocks_nondomestic_inference(self) -> None:
        # #4: sovereign 下非国产模型推理须在出网前被拒, adapter 不应被调用
        _patch_adapter(self, _AlwaysToolAdapter(), is_real=True)
        spec = get_model("claude")   # provider=anthropic, domestic=False
        events = await run_loop(self.conn, user_id="u-h", convo=[{"role": "user", "content": "q"}],
                                allowed_tools=["vault_search"], spec=spec,
                                run_id="r-sov", privacy_tier="sovereign")
        self.assertTrue(any(e["type"] == "egress_denied" for e in events))
        self.assertEqual([e for e in events if e["type"] in ("tool_call", "delta")], [])
        self.assertEqual(dal.list_agent_steps(self.conn, "r-sov"), [])

    async def test_sovereign_domestic_inference_audited(self) -> None:
        # #15: sovereign + 国产真实出网 → model.infer 审计 egress_flag=1
        _patch_adapter(self, _PureTextAdapter(), is_real=True)
        await run_loop(self.conn, user_id="u-h", convo=[{"role": "user", "content": "q"}],
                       allowed_tools=[], spec=self.spec, run_id="r-aud", privacy_tier="sovereign")
        row = self.conn.execute(
            "SELECT egress_flag FROM audit_log WHERE user_id='u-h' AND action='model.infer'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)

    async def test_multiple_tool_calls_one_step(self) -> None:
        # #17: 同回合多调用 — 一准一拒, 各产事件, step.tool 逗号拼
        convo = [{"role": "user", "content": "q"}]
        _patch_adapter(self, _MultiCallAdapter(), is_real=False)
        events = await run_loop(self.conn, user_id="u-h", convo=convo,
                                allowed_tools=["vault_search"], spec=self.spec, run_id="r-multi")
        self.assertEqual(len([e for e in events if e["type"] == "tool_call"]), 2)
        self.assertTrue(any(e["type"] == "tool_result" and e["name"] == "vault_search" for e in events))
        self.assertTrue(any(e["type"] == "tool_denied" and e["name"] == "vault_archive" for e in events))
        tool_msgs = [m for m in convo if m["role"] == "tool"]
        self.assertEqual({m["tool_call_id"] for m in tool_msgs}, {"c-a", "c-b"})
        self.assertEqual(dal.list_agent_steps(self.conn, "r-multi")[0]["tool"], "vault_search,vault_archive")

    async def test_tool_error_observation_and_status(self) -> None:
        # #14/#20: 工具抛错 → tool_result 带 error, step.status='tool_error', 循环不崩
        _patch_adapter(self, _MultiCallAdapter(), is_real=False)
        self.addCleanup(setattr, agent_loop.tools, "execute_call", agent_loop.tools.execute_call)

        def _boom(tool, conn, user_id, args, **kw):
            raise RuntimeError("boom")
        agent_loop.tools.execute_call = _boom
        events = await run_loop(self.conn, user_id="u-h", convo=[{"role": "user", "content": "q"}],
                                allowed_tools=["vault_search"], spec=self.spec, run_id="r-err")
        res = next(e for e in events if e["type"] == "tool_result" and e["name"] == "vault_search")
        self.assertEqual(res["result"]["error"], "tool_error")
        self.assertEqual(dal.list_agent_steps(self.conn, "r-err")[0]["status"], "tool_error")

    async def test_pure_text_final_deterministic(self) -> None:
        # #18: 纯文本桩 → 恰一个 final step, 无工具事件, 一行 tool IS NULL
        _patch_adapter(self, _PureTextAdapter(), is_real=False)
        events = await run_loop(self.conn, user_id="u-h", convo=[{"role": "user", "content": "q"}],
                                allowed_tools=["vault_search"], spec=self.spec, run_id="r-final")
        self.assertEqual(len([e for e in events if e["type"] == "step" and e.get("kind") == "final"]), 1)
        self.assertEqual([e for e in events if e["type"] in ("tool_call", "tool_result")], [])
        steps = dal.list_agent_steps(self.conn, "r-final")
        self.assertEqual(len(steps), 1)
        self.assertIsNone(steps[0]["tool"])

    async def test_parent_message_id_and_args_hash_persisted(self) -> None:
        # #16: parent_message_id 落每行; args_hash 记录工具步入参指纹
        _patch_adapter(self, _UsageToolAdapter(), is_real=False)
        await run_loop(self.conn, user_id="u-h", convo=[{"role": "user", "content": "q"}],
                       allowed_tools=["vault_search"], spec=self.spec, run_id="r-pid",
                       parent_message_id="m-parent")
        rows = dal.list_agent_steps(self.conn, "r-pid")
        self.assertTrue(rows)
        self.assertTrue(all(r["parent_message_id"] == "m-parent" for r in rows))
        self.assertTrue(rows[0]["args_hash"], "工具步须记 args_hash")

    async def test_step_limit_fields_uniform(self) -> None:
        # #11: 两种 step_limit 均带 step + max
        _patch_adapter(self, _AlwaysToolAdapter(), is_real=False)
        events = await run_loop(self.conn, user_id="u-h", convo=[{"role": "user", "content": "q"}],
                                allowed_tools=["vault_search"], spec=self.spec,
                                run_id="r-lim", max_steps=2)
        lim = next(e for e in events if e["type"] == "step_limit")
        self.assertIn("step", lim)
        self.assertIn("max", lim)

    async def test_tool_denied_carries_args(self) -> None:
        # #12: tool_denied 与 tool_call 对称 (带 args + reason)
        convo = [{"role": "user", "content": "归档 [[mock-tool:vault_archive]]"}]
        events = await run_loop(self.conn, user_id="u-h", convo=convo,
                                allowed_tools=["vault_search"], spec=self.spec, run_id="r-den2")
        den = next(e for e in events if e["type"] == "tool_denied")
        self.assertIn("args", den)
        self.assertIn("reason", den)


class ExportDeleteAgentStepsTest(unittest.TestCase):
    """#7/#8: agent_steps 纳入导出 + 级联删除受体 (数据主权契约)。"""

    def test_export_includes_agent_steps(self) -> None:
        conn = fresh_conn()
        dal.ensure_user(conn, "u-x", "u-x@mozi.local", "CN")
        dal.record_agent_step(conn, run_id="r", user_id="u-x", step_idx=0, tool="vault_search")
        export = dal.export_user_data(conn, "u-x")
        self.assertIn("agent_steps", export)
        self.assertEqual(len(export["agent_steps"]), 1)

    def test_delete_user_data_counts_agent_steps(self) -> None:
        from mozi_backend.db import database
        conn = fresh_conn()
        dal.ensure_user(conn, "u-d", "u-d@mozi.local", "CN")
        dal.record_agent_step(conn, run_id="r", user_id="u-d", step_idx=0, tool="vault_search")
        with database.transaction(conn):
            counts = dal.delete_user_data(conn, "u-d")
        self.assertEqual(counts.get("agent_steps"), 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM agent_steps").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
