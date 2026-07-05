"""EPIC-ADAPTER: 幂等窗口重试 (首字节前重试 / 首字节后不重试 / 耗尽上抛)。

直测纯 Python 兜底驱动 _drive_with_fallback (stamina 缺失时的权威路径), 与 transport 级集成。
"""
from __future__ import annotations

import asyncio
import unittest

import httpx

try:
    from ._helpers import collect_astream, make_mock_transport, sse_body
except ImportError:
    from _helpers import collect_astream, make_mock_transport, sse_body

from mozi_backend.gateway.adapters import _resilient  # noqa: E402
from mozi_backend.gateway.adapters.anthropic import AnthropicAdapter  # noqa: E402
from mozi_backend.gateway.adapters.openai_compat import OpenAICompatAdapter  # noqa: E402
from mozi_backend.gateway.models import get_model  # noqa: E402

CONVO = [{"role": "user", "content": "你好"}]


def _delta_frame(content: str) -> str:
    return '{"choices":[{"index":0,"delta":{"content":"%s"}}]}' % content


async def _collect(agen) -> list[str]:
    return [d async for d in agen]


class DriveFallbackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._base = _resilient.RETRY_BASE_DELAY_S
        _resilient.RETRY_BASE_DELAY_S = 0.0   # 免退避 sleep

    def tearDown(self) -> None:
        _resilient.RETRY_BASE_DELAY_S = self._base

    async def test_retry_transient_then_success(self) -> None:
        builds = {"n": 0}

        async def make_attempt():
            builds["n"] += 1
            if builds["n"] == 1:
                raise httpx.ReadTimeout("transient")   # 首字节前可重试异常
            yield "a"
            yield "b"

        out = await _collect(_resilient._drive_with_fallback(make_attempt))
        self.assertEqual(out, ["a", "b"])
        self.assertEqual(builds["n"], 2, "首字节前瞬时异常应重试一次")

    async def test_no_retry_after_first_byte(self) -> None:
        builds = {"n": 0}

        async def make_attempt():
            builds["n"] += 1
            yield "half"
            raise httpx.ReadTimeout("mid-stream")       # 首字节后异常

        got: list[str] = []
        with self.assertRaises(httpx.ReadTimeout):
            async for d in _resilient._drive_with_fallback(make_attempt):
                got.append(d)
        self.assertEqual(got, ["half"])
        self.assertEqual(builds["n"], 1, "首字节后绝不重试 (防重复计费)")

    async def test_non_retryable_immediate_raise(self) -> None:
        builds = {"n": 0}

        async def make_attempt():
            builds["n"] += 1
            raise ValueError("deterministic")
            yield  # pragma: no cover

        with self.assertRaises(ValueError):
            await _collect(_resilient._drive_with_fallback(make_attempt))
        self.assertEqual(builds["n"], 1, "不可重试异常立即上抛, 不重试")


class TransportRetryIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._base = _resilient.RETRY_BASE_DELAY_S
        _resilient.RETRY_BASE_DELAY_S = 0.0

    def tearDown(self) -> None:
        _resilient.RETRY_BASE_DELAY_S = self._base

    async def test_connect_error_retried_via_transport(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("transient")
            return httpx.Response(200, text=sse_body(_delta_frame("ok"), "[DONE]"),
                                  headers={"content-type": "text/event-stream"})

        text = await collect_astream(OpenAICompatAdapter(), get_model("glm-5.2"), CONVO,
                                     {}, transport=make_mock_transport(handler))
        self.assertEqual(text, "ok")
        self.assertEqual(calls["n"], 2, "建连瞬时失败应重试一次后成功")


class _StallStream(httpx.AsyncByteStream):
    """上游逐帧吐字, 帧间 sleep(stall) 模拟慢上游 (用于上游墙钟边界测试)。"""

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


class UpstreamTimeoutDecoupleTest(unittest.IsolatedAsyncioTestCase):
    """消解 MAJOR: 上游墙钟只钳「读下一行」, 与下游回压解耦。"""

    def setUp(self) -> None:
        self._orig = _resilient.UPSTREAM_READ_DEADLINE_S
        _resilient.UPSTREAM_READ_DEADLINE_S = 0.05   # 极小上游墙钟便于触发

    def tearDown(self) -> None:
        _resilient.UPSTREAM_READ_DEADLINE_S = self._orig

    async def test_slow_consumer_does_not_trip_upstream_timeout(self) -> None:
        # 上游缓冲即达 (无帧间停顿), 消费者每 delta sleep 0.1s > 上游墙钟 0.05s。
        # 解耦正确则全部 delta 收齐、不误触上游超时。
        body = sse_body(_delta_frame("a"), _delta_frame("b"), _delta_frame("c"), "[DONE]")
        tr = make_mock_transport(
            lambda req: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
        got: list[str] = []
        async for d in OpenAICompatAdapter().astream(get_model("glm-5.2"), CONVO, {}, transport=tr):
            got.append(d)
            await asyncio.sleep(0.1)                  # 慢消费者: 下游回压 (墙钟外)
        self.assertEqual(got, ["a", "b", "c"], "慢消费者不得误触发上游超时降级")

    async def test_upstream_stall_between_lines_triggers_timeout(self) -> None:
        # 上游首帧后停吐超过墙钟 → 读下一行的 asyncio.timeout 触发 TimeoutError 上抛。
        chunks = [f"data: {_delta_frame('a')}\n\n".encode(), f"data: {_delta_frame('b')}\n\n".encode()]
        tr = make_mock_transport(
            lambda req: httpx.Response(200, stream=_StallStream(chunks, stall=0.2),
                                       headers={"content-type": "text/event-stream"}))
        got: list[str] = []
        with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
            async for d in OpenAICompatAdapter().astream(get_model("glm-5.2"), CONVO, {}, transport=tr):
                got.append(d)
        self.assertEqual(got, ["a"], "首帧已吐, 第二行停顿超墙钟才超时")


class RetryBillingIsolationTest(unittest.IsolatedAsyncioTestCase):
    """消解 MINOR-计费: 首字节前部分 usage 已写 → 可重试异常 → 重试成功的完整 usage 覆盖,
    最终计量取末次 attempt 值, 不被首次半截 usage 污染。"""

    def setUp(self) -> None:
        self._dl = _resilient.UPSTREAM_READ_DEADLINE_S
        _resilient.UPSTREAM_READ_DEADLINE_S = 0.05
        self._base = _resilient.RETRY_BASE_DELAY_S
        _resilient.RETRY_BASE_DELAY_S = 0.0

    def tearDown(self) -> None:
        _resilient.UPSTREAM_READ_DEADLINE_S = self._dl
        _resilient.RETRY_BASE_DELAY_S = self._base

    async def test_partial_usage_not_polluting_retry_success(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                # attempt1: 先吐 message_start(写 tokens_in=11, 无 delta → emitted=False), 再停吐超墙钟 → 超时重试
                chunks = [b'data: {"type":"message_start","message":{"usage":{"input_tokens":11}}}\n\n',
                          b'data: {"type":"content_block_delta","delta":{"text":"x"}}\n\n']
                return httpx.Response(200, stream=_StallStream(chunks, stall=0.2),
                                     headers={"content-type": "text/event-stream"})
            # attempt2: 完整成功, usage 为末次真值
            body = sse_body(
                '{"type":"message_start","message":{"usage":{"input_tokens":22}}}',
                '{"type":"content_block_delta","delta":{"text":"hi"}}',
                '{"type":"message_delta","usage":{"output_tokens":33}}',
                '{"type":"message_stop"}')
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        usage: dict = {}
        text = await collect_astream(AnthropicAdapter(), get_model("claude"), CONVO, usage,
                                     transport=make_mock_transport(handler))
        self.assertEqual(text, "hi")
        self.assertEqual(calls["n"], 2, "首字节前超时应重试一次")
        self.assertEqual(usage.get("tokens_in"), 22, "计量取末次 attempt, 非首次半截 11")
        self.assertEqual(usage.get("tokens_out"), 33)


if __name__ == "__main__":
    unittest.main()
