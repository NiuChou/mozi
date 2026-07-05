"""PR1 (P0-A 阶段1): 适配层 tool-calling 能力。零外呼, httpx MockTransport 驱动。

覆盖:
- openai_compat: 流式 tool_calls 跨帧分片累积 + finish_reason=tool_calls → ToolCallsReady。
- 坏 args JSON → 降级空 dict (循环把错误当观测回灌, 不崩)。
- mock: 确定性 tool-call 模式 (input 埋 trigger → 产 ToolCallsReady; 已有观测 → 出文本)。
- tools_schema: allowed ∩ 注册表 → OpenAI function schema。
- 文本路径回归: 无 tools 时 astream 仍只 yield str (旧行为逐字不变)。
"""
from __future__ import annotations

import asyncio
import unittest

import httpx

try:  # 兼容两种运行方式
    from ._helpers import fresh_conn, make_mock_transport, sse_body
except ImportError:
    from _helpers import fresh_conn, make_mock_transport, sse_body

from mozi_backend.gateway.adapters import _resilient  # noqa: E402
from mozi_backend.gateway.adapters.base import ToolCall, ToolCallsReady  # noqa: E402
from mozi_backend.gateway.adapters.mock import MockAdapter  # noqa: E402
from mozi_backend.gateway.adapters.openai_compat import OpenAICompatAdapter  # noqa: E402
from mozi_backend.gateway.models import get_model  # noqa: E402

CONVO = [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "查一下墨子"}]


async def collect_events(adapter, spec, convo, *, tools=None, transport=None, usage=None):
    """收集 astream 的全部产出 (str 文本 delta 与 ToolCallsReady 事件混合)。"""
    out: list = []
    async for ev in adapter.astream(spec, convo, usage if usage is not None else {},
                                    tools=tools, transport=transport):
        out.append(ev)
    return out


def _tc_frame(*, index=0, id=None, name=None, args=None, finish=None) -> str:
    """构造一帧 OpenAI chat.completion.chunk 的 tool_calls 增量 (或 finish 帧)。"""
    import json
    delta: dict = {}
    if id is not None or name is not None or args is not None:
        fn: dict = {}
        if name is not None:
            fn["name"] = name
        if args is not None:
            fn["arguments"] = args
        tc: dict = {"index": index}
        if id is not None:
            tc["id"] = id
        if fn:
            tc["function"] = fn
        delta["tool_calls"] = [tc]
    choice: dict = {"index": 0, "delta": delta}
    if finish is not None:
        choice["finish_reason"] = finish
    return json.dumps({"choices": [choice]})


class OpenAIToolCallParseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = OpenAICompatAdapter()
        self.spec = get_model("glm-5.2")

    async def test_openai_toolcall_parse(self) -> None:
        # tool_call 跨 3 帧分片到达: 先 id+name, 再 args 两段, 末帧 finish_reason=tool_calls
        frames = [
            _tc_frame(id="call_abc", name="vault_search", args=""),
            _tc_frame(args='{"query":'),
            _tc_frame(args='"墨子"}'),
            _tc_frame(finish="tool_calls"),
            "[DONE]",
        ]
        transport = make_mock_transport(
            lambda req: httpx.Response(200, text=sse_body(*frames),
                                       headers={"content-type": "text/event-stream"}))
        schema = [{"type": "function", "function": {"name": "vault_search",
                                                    "parameters": {"type": "object"}}}]
        events = await collect_events(self.adapter, self.spec, CONVO,
                                      tools=schema, transport=transport)

        ready = [e for e in events if isinstance(e, ToolCallsReady)]
        self.assertEqual(len(ready), 1, "须产出恰好 1 个 ToolCallsReady")
        calls = ready[0].calls
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0], ToolCall)
        self.assertEqual(calls[0].id, "call_abc")
        self.assertEqual(calls[0].name, "vault_search")
        self.assertEqual(calls[0].args, {"query": "墨子"}, "跨帧 args 分片须拼成完整 JSON")
        # 不应有文本 delta (本回合纯工具调用)
        self.assertEqual([e for e in events if isinstance(e, str)], [])


class _StallStream(httpx.AsyncByteStream):
    """首帧后停吐超墙钟 → 读下一行的 asyncio.timeout 触发可重试超时 (模拟中途断流)。"""

    def __init__(self, chunks: list[bytes], stall: float) -> None:
        self._chunks = chunks
        self._stall = stall

    async def __aiter__(self):
        for i, ch in enumerate(self._chunks):
            if i > 0:
                await asyncio.sleep(self._stall)
            yield ch

    async def aclose(self) -> None:
        return None


class ToolCallRetryResetTest(unittest.IsolatedAsyncioTestCase):
    """回归: attempt1 吐半截 args 后可重试断流, attempt2 干净重放 → 累积器须每 attempt 清态,
    否则重试拼在前次半截上 → 坏 JSON 被 _safe_json 吞成 {}, 工具拿空参执行。"""

    def setUp(self) -> None:
        self.adapter = OpenAICompatAdapter()
        self.spec = get_model("glm-5.2")
        self._dl = _resilient.UPSTREAM_READ_DEADLINE_S
        _resilient.UPSTREAM_READ_DEADLINE_S = 0.05    # 极小上游墙钟便于触发中途断流
        self._base = _resilient.RETRY_BASE_DELAY_S
        _resilient.RETRY_BASE_DELAY_S = 0.0

    def tearDown(self) -> None:
        _resilient.UPSTREAM_READ_DEADLINE_S = self._dl
        _resilient.RETRY_BASE_DELAY_S = self._base

    async def test_midargs_drop_then_clean_retry_yields_clean_args(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                # attempt1: 首帧即 id+name+半截 args '{"q":"' 落进 acc (emitted=False, 无 content/finish),
                # 之后停吐超墙钟 → 可重试 TimeoutError。半截 '{"q":"' 已在累积器里。
                chunks = [
                    f"data: {_tc_frame(id='call_x', name='vault_search', args='{\"q\":\"')}\n\n".encode(),
                    f"data: {_tc_frame(args='STALE')}\n\n".encode(),
                ]
                return httpx.Response(200, stream=_StallStream(chunks, stall=0.2),
                                     headers={"content-type": "text/event-stream"})
            # attempt2: 干净重放 → 若 acc 未清则拼成 '{"q":"STALE{"q":"hi"}' 坏 JSON
            frames = [
                _tc_frame(id="call_x", name="vault_search", args=""),
                _tc_frame(args='{"q":"hi"}'),
                _tc_frame(finish="tool_calls"),
                "[DONE]",
            ]
            return httpx.Response(200, text=sse_body(*frames),
                                  headers={"content-type": "text/event-stream"})

        schema = [{"type": "function", "function": {"name": "vault_search",
                                                    "parameters": {"type": "object"}}}]
        events = await collect_events(self.adapter, self.spec, CONVO,
                                      tools=schema, transport=make_mock_transport(handler))
        self.assertEqual(calls["n"], 2, "attempt1 中途断流应重试一次")
        ready = [e for e in events if isinstance(e, ToolCallsReady)]
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].calls[0].args, {"q": "hi"},
                         "重试前须清累积器, args 须为干净 {'q':'hi'} 而非拼半截的坏 JSON")


class MockToolCallTest(unittest.IsolatedAsyncioTestCase):
    """mock 确定性 tool-call: input 埋 trigger → 产 ToolCallsReady; 已有观测 → 出文本收尾。"""

    def setUp(self) -> None:
        self.adapter = MockAdapter()
        self.spec = get_model("glm-5.2")

    async def test_mock_emits_toolcall_on_trigger(self) -> None:
        convo = [{"role": "user", "content": "请联网 [[mock-tool:web_search]] 查最新"}]
        schema = [{"type": "function", "function": {"name": "web_search"}}]
        events = await collect_events(self.adapter, self.spec, convo, tools=schema)
        ready = [e for e in events if isinstance(e, ToolCallsReady)]
        self.assertEqual(len(ready), 1, "trigger 命中须产 1 个 ToolCallsReady")
        self.assertEqual(ready[0].calls[0].name, "web_search")
        self.assertEqual([e for e in events if isinstance(e, str)], [], "本回合纯工具调用, 无文本")

    async def test_mock_emits_text_when_observation_present(self) -> None:
        # 已有 role:tool 观测 → 收尾出文本 (循环确定性收敛, 不再调工具)
        convo = [{"role": "user", "content": "请联网 [[mock-tool:web_search]] 查最新"},
                 {"role": "assistant", "tool_call": "web_search"},
                 {"role": "tool", "name": "web_search", "content": "{\"results\": []}"}]
        schema = [{"type": "function", "function": {"name": "web_search"}}]
        events = await collect_events(self.adapter, self.spec, convo, tools=schema)
        self.assertEqual([e for e in events if isinstance(e, ToolCallsReady)], [],
                         "有观测则不再产工具调用")
        self.assertTrue([e for e in events if isinstance(e, str)], "须出文本收尾")

    async def test_mock_no_tools_pure_text(self) -> None:
        # 无 tools → 旧纯文本行为逐字不变 (即便 input 含 trigger 也只出文本)
        convo = [{"role": "user", "content": "请联网 [[mock-tool:web_search]] 查最新"}]
        events = await collect_events(self.adapter, self.spec, convo, tools=None)
        self.assertEqual([e for e in events if isinstance(e, ToolCallsReady)], [])
        self.assertTrue(all(isinstance(e, str) for e in events))


class ToolsSchemaTest(unittest.IsolatedAsyncioTestCase):
    """tools_schema: allowed ∩ 注册表 → OpenAI function schema; execute_call: args(dict) 桥接 fn(query)。"""

    async def test_tools_schema_skips_unregistered(self) -> None:
        from mozi_backend.skills import tools as t
        schema = t.tools_schema(["vault_search", "kg_query", "definitely_not_a_tool"])
        names = [s["function"]["name"] for s in schema]
        self.assertEqual(names, ["vault_search", "kg_query"], "未注册工具须被跳过")
        for s in schema:
            self.assertEqual(s["type"], "function")
            self.assertIn("query", s["function"]["parameters"]["properties"])

    async def test_execute_call_bridges_args_to_query(self) -> None:
        from mozi_backend.db import dal
        from mozi_backend.skills import tools as t
        conn = fresh_conn()
        dal.ensure_user(conn, "u-tc", "u-tc@mozi.local", "CN")
        # vault_search 是只读工具, 空库返回 0 命中 (确定性, 无外呼)
        res = t.execute_call("vault_search", conn, "u-tc", {"query": "墨子"})
        self.assertEqual(res["tool"], "vault_search")
        self.assertEqual(res["query"], "墨子", "args.query 须桥接为 fn 的 query 入参")
        self.assertEqual(res["count"], 0)


if __name__ == "__main__":
    unittest.main()
