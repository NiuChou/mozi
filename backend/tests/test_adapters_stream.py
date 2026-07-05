"""test-adequacy #3: 真实适配器 astream (httpx 内置 MockTransport, 零外呼)。

openai_compat: 200 多帧 SSE → delta 拼接 + include_usage 解析 tokens;
               500 / 坏 JSON / [DONE] → 抛异常 / 跳过不崩。
anthropic:     message_start/content_block_delta/message_delta → 文本拼接 + 用量解析。
"""
from __future__ import annotations

import json
import os
import unittest

import httpx

try:  # 兼容 `discover -s tests` (顶层) 与 `unittest tests.X` (包) 两种运行方式
    from ._helpers import collect_astream, make_mock_transport, mock_async_client_factory, sse_body
except ImportError:
    from _helpers import collect_astream, make_mock_transport, mock_async_client_factory, sse_body

from mozi_backend.gateway.adapters import anthropic as anthropic_mod  # noqa: E402
from mozi_backend.gateway.adapters import openai_compat as oai_mod  # noqa: E402
from mozi_backend.gateway.adapters.anthropic import AnthropicAdapter  # noqa: E402
from mozi_backend.gateway.adapters.openai_compat import OpenAICompatAdapter  # noqa: E402
from mozi_backend.gateway.models import get_model  # noqa: E402

CONVO = [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "你好"}]


def _delta_frame(content: str) -> str:
    return '{"choices":[{"index":0,"delta":{"content":"%s"}}]}' % content


class OpenAICompatStreamTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = OpenAICompatAdapter()
        self.spec = get_model("glm-5.2")
        self._orig = oai_mod.httpx.AsyncClient
        # provider.api_key 经 os.getenv 实时读取; 仅在本用例期注入, tearDown 清除,
        # 避免污染同进程其它测试模块的 settings.active_providers() (致其误走真实出网)。
        self._prev_key = os.environ.get("GLM_API_KEY")
        os.environ["GLM_API_KEY"] = "test-key-glm"

    def tearDown(self) -> None:
        oai_mod.httpx.AsyncClient = self._orig
        if self._prev_key is None:
            os.environ.pop("GLM_API_KEY", None)
        else:
            os.environ["GLM_API_KEY"] = self._prev_key

    def _patch(self, handler) -> None:
        oai_mod.httpx.AsyncClient = mock_async_client_factory(httpx.MockTransport(handler))

    async def test_multi_frame_delta_concat_and_usage(self) -> None:
        # 200 + 多帧 SSE (含 include_usage 末帧)
        frames = [_delta_frame("墨"), _delta_frame("子"), _delta_frame("很"), _delta_frame("好"),
                  '{"choices":[],"usage":{"prompt_tokens":123,"completion_tokens":45}}', "[DONE]"]

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            body = request.read().decode()
            captured["stream_options"] = '"include_usage": true' in body or '"include_usage":true' in body
            return httpx.Response(200, text=sse_body(*frames),
                                  headers={"content-type": "text/event-stream"})

        self._patch(handler)
        usage: dict = {}
        text = await collect_astream(self.adapter, self.spec, CONVO, usage)

        self.assertEqual(text, "墨子很好", "多帧 delta 必须按序拼接")
        self.assertEqual(usage.get("tokens_in"), 123, "include_usage 须解析 prompt_tokens")
        self.assertEqual(usage.get("tokens_out"), 45, "include_usage 须解析 completion_tokens")
        # 真实出网细节: 正确 base_url + Bearer 头 + 请求了 include_usage
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual(captured["auth"], "Bearer test-key-glm")
        self.assertTrue(captured["stream_options"], "payload 须带 stream_options.include_usage")

    async def test_bad_json_and_done_skipped_no_crash(self) -> None:
        # 坏 JSON 帧须跳过; 非 data: 行须忽略; [DONE] 须终止; 有效帧仍拼接
        frames = ["{not json", _delta_frame("A"), "  ", _delta_frame("B"), "[DONE]", _delta_frame("C")]
        body = sse_body(*frames) + ": this is a comment line\n\n"
        self._patch(lambda req: httpx.Response(200, text=body,
                                               headers={"content-type": "text/event-stream"}))
        text = await collect_astream(self.adapter, self.spec, CONVO, {})
        self.assertEqual(text, "AB", "坏 JSON 跳过、[DONE] 后帧不再读取")

    async def test_empty_choices_frame_no_index_error(self) -> None:
        # choices 为空的中间帧 (仅用量) 不得越界崩溃
        frames = ['{"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}',
                  _delta_frame("X"), "[DONE]"]
        self._patch(lambda req: httpx.Response(200, text=sse_body(*frames),
                                               headers={"content-type": "text/event-stream"}))
        usage: dict = {}
        text = await collect_astream(self.adapter, self.spec, CONVO, usage)
        self.assertEqual(text, "X")
        self.assertEqual(usage["tokens_in"], 1)

    async def test_http_500_raises(self) -> None:
        # 5xx → raise_for_status 抛 HTTPStatusError (上层据此降级)
        self._patch(lambda req: httpx.Response(500, text="upstream boom"))
        with self.assertRaises(httpx.HTTPStatusError):
            await collect_astream(self.adapter, self.spec, CONVO, {})

    async def test_falsy_zero_usage_preserved(self) -> None:
        # 0 是合法用量值, 必须被写入 (回归: 不可被 falsy 吞掉)
        frames = [_delta_frame("Z"),
                  '{"choices":[],"usage":{"prompt_tokens":0,"completion_tokens":0}}', "[DONE]"]
        self._patch(lambda req: httpx.Response(200, text=sse_body(*frames),
                                               headers={"content-type": "text/event-stream"}))
        usage: dict = {}
        await collect_astream(self.adapter, self.spec, CONVO, usage)
        self.assertEqual(usage.get("tokens_in"), 0)
        self.assertEqual(usage.get("tokens_out"), 0)


class AnthropicStreamTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = AnthropicAdapter()
        self.spec = get_model("claude")
        self._orig = anthropic_mod.httpx.AsyncClient
        self._prev_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-key-anthropic"

    def tearDown(self) -> None:
        anthropic_mod.httpx.AsyncClient = self._orig
        if self._prev_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._prev_key

    def _patch(self, handler) -> None:
        anthropic_mod.httpx.AsyncClient = mock_async_client_factory(httpx.MockTransport(handler))

    async def test_message_start_delta_usage_and_text(self) -> None:
        frames = [
            '{"type":"message_start","message":{"usage":{"input_tokens":88}}}',
            '{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}',
            '{"type":"content_block_delta","delta":{"type":"text_delta","text":" 世界"}}',
            '{"type":"message_delta","usage":{"output_tokens":17}}',
            '{"type":"message_stop"}',
        ]

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["xkey"] = request.headers.get("x-api-key")
            body = request.read().decode()
            # system 须从 messages 抽出放 top-level system
            captured["has_system"] = '"system"' in body
            return httpx.Response(200, text=sse_body(*frames),
                                  headers={"content-type": "text/event-stream"})

        self._patch(handler)
        usage: dict = {}
        text = await collect_astream(self.adapter, self.spec, CONVO, usage)

        self.assertEqual(text, "Hello 世界", "content_block_delta 文本须按序拼接")
        self.assertEqual(usage.get("tokens_in"), 88, "message_start 须解析 input_tokens")
        self.assertEqual(usage.get("tokens_out"), 17, "message_delta 须解析 output_tokens")
        self.assertTrue(captured["url"].endswith("/messages"))
        self.assertEqual(captured["xkey"], "test-key-anthropic")
        self.assertTrue(captured["has_system"], "system 消息须抽出为 top-level system 字段")

    async def test_bad_json_skipped(self) -> None:
        frames = ["<<garbage",
                  '{"type":"content_block_delta","delta":{"text":"ok"}}',
                  '{"type":"message_stop"}']
        self._patch(lambda req: httpx.Response(200, text=sse_body(*frames),
                                               headers={"content-type": "text/event-stream"}))
        text = await collect_astream(self.adapter, self.spec, CONVO, {})
        self.assertEqual(text, "ok")

    async def test_http_500_raises(self) -> None:
        self._patch(lambda req: httpx.Response(500, text="boom"))
        with self.assertRaises(httpx.HTTPStatusError):
            await collect_astream(self.adapter, self.spec, CONVO, {})


class TransportInjectionTest(unittest.IsolatedAsyncioTestCase):
    """正式 transport 注入路径 (不依赖 monkeypatch) + MiniMax 端点/模型名可配置。"""

    async def test_minimax_endpoint_and_model_via_transport(self) -> None:
        from mozi_backend.gateway.models import PROVIDER_MODEL_NAME
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["model"] = json.loads(request.read().decode()).get("model")
            return httpx.Response(200, text=sse_body(_delta_frame("好"), "[DONE]"),
                                  headers={"content-type": "text/event-stream"})

        text = await collect_astream(OpenAICompatAdapter(), get_model("minimax-m3"), CONVO, {},
                                     transport=make_mock_transport(handler))
        self.assertEqual(text, "好")
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual(captured["model"], PROVIDER_MODEL_NAME["minimax-m3"])
        self.assertEqual(PROVIDER_MODEL_NAME["minimax-m3"], "MiniMax-Text-01",
                         "默认 MiniMax 模型名 (MOZI_MINIMAX_MODEL 可覆盖)")


class AnthropicMaxTokensTest(unittest.IsolatedAsyncioTestCase):
    async def test_max_tokens_driven_by_spec(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["max_tokens"] = json.loads(request.read().decode()).get("max_tokens")
            return httpx.Response(200, text=sse_body(
                '{"type":"content_block_delta","delta":{"text":"hi"}}',
                '{"type":"message_stop"}'), headers={"content-type": "text/event-stream"})

        text = await collect_astream(AnthropicAdapter(), get_model("claude"), CONVO, {},
                                     transport=make_mock_transport(handler))
        self.assertEqual(text, "hi")
        self.assertEqual(captured["max_tokens"], 8192, "claude spec.max_output=8192 驱动, 非写死 2048")

    async def test_accepts_and_ignores_tools_kwarg(self) -> None:
        # 回归: agentic 循环对 Claude 传 tools=schema, 曾因签名缺 tools 抛 TypeError → /skills/invoke 500。
        # Anthropic function-calling 未接线, 应收下并忽略, 仍仅 yield 文本。
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=sse_body(
                '{"type":"content_block_delta","delta":{"text":"hi"}}',
                '{"type":"message_stop"}'), headers={"content-type": "text/event-stream"})

        out = [d async for d in AnthropicAdapter().astream(
            get_model("claude"), CONVO, {}, tools=[{"name": "x"}],
            transport=make_mock_transport(handler))]
        self.assertEqual(out, ["hi"], "tools 被忽略, 仍仅产出文本 str")


if __name__ == "__main__":
    unittest.main()
