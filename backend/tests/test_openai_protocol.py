"""EPIC-P0-2: OpenAI 兼容出站 (chunk SSE / JSON 聚合) + 修全降级吞错/stream_reset 污染。"""
from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

try:
    from ._helpers import fresh_conn  # noqa: F401  触发临时库环境
except ImportError:
    from _helpers import fresh_conn  # noqa: F401

from mozi_backend.gateway.openai_protocol import (  # noqa: E402
    aggregate_openai_json,
    to_openai_sse,
)
from mozi_backend.main import app  # noqa: E402


async def _agen(events):
    for e in events:
        yield e


def _objs(lines: list[str]) -> list:
    out = []
    for ln in lines:
        body = ln[len("data: "):].strip()
        if body and body != "[DONE]":
            out.append(json.loads(body))
    return out


class ToOpenAISseTest(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, events, **kw):
        return [ln async for ln in to_openai_sse(_agen(events), **kw)]

    async def test_basic_chunks(self):
        lines = await self._collect([
            {"type": "routing_metadata", "chosen_model": "glm-5.2"},
            {"type": "delta", "text": "你"}, {"type": "delta", "text": "好"},
            {"type": "usage", "prompt_tokens": 3, "completion_tokens": 1, "model": "glm-5.2"},
            {"type": "done", "message_id": "m", "session_id": "s"}])
        self.assertEqual(lines[-1], "data: [DONE]\n\n")
        objs = _objs(lines)
        self.assertTrue(all(o["object"] == "chat.completion.chunk" for o in objs))
        # 首个 content 帧带 role=assistant
        content = next(o for o in objs if o["choices"][0]["delta"].get("content"))
        self.assertEqual(content["choices"][0]["delta"].get("role"), "assistant")
        self.assertEqual(content["choices"][0]["delta"]["content"], "你好")
        last = objs[-1]
        self.assertEqual(last["choices"][0]["finish_reason"], "stop")
        self.assertEqual(last["usage"]["total_tokens"], 4)

    async def test_stream_reset_no_pollution(self):
        lines = await self._collect([
            {"type": "routing_metadata", "chosen_model": "a"},
            {"type": "delta", "text": "半截"},
            {"type": "stream_reset", "from_model": "a"},
            {"type": "routing_metadata", "chosen_model": "b"},
            {"type": "delta", "text": "完整答案"},
            {"type": "usage", "prompt_tokens": 1, "completion_tokens": 2, "model": "b"},
            {"type": "done", "message_id": "m", "session_id": "s"}])
        content = "".join(o["choices"][0]["delta"].get("content", "")
                          for o in _objs(lines) if o.get("choices"))
        self.assertEqual(content, "完整答案", "stream_reset 后作废半截不得污染")
        self.assertNotIn("半截", content)

    async def test_all_fallback_failed_stream_error(self):
        lines = await self._collect([
            {"type": "retrieval", "injected": False, "hits": []},
            {"type": "routing_metadata", "chosen_model": "a"},
            {"type": "delta", "text": "半截"},
            {"type": "stream_reset", "from_model": "a"},
            {"type": "fallback", "from_model": "a", "to_model": "n/a"},
            {"type": "error", "detail": "所有模型降级均失败", "code": "quota_exceeded"}])
        objs = _objs(lines)
        self.assertTrue(any("error" in o for o in objs), "须含 error 帧")
        self.assertFalse(any(c.get("finish_reason") == "stop"
                             for o in objs for c in o.get("choices", [])),
                         "全降级失败绝不发 finish_reason=stop")
        self.assertEqual(lines[-1], "data: [DONE]\n\n")
        content = "".join(c["delta"].get("content", "")
                          for o in objs for c in o.get("choices", []))
        self.assertEqual(content, "", "失败不得外泄半截 content")

    async def test_strict_drops_supersets(self):
        lines = await self._collect([
            {"type": "retrieval", "injected": False, "hits": []},
            {"type": "delta", "text": "x"},
            {"type": "done", "message_id": "m", "session_id": "s"}], strict=True)
        self.assertFalse(any("x_mozi" in o for o in _objs(lines)), "strict 丢弃超集事件")


class AggregateJsonTest(unittest.IsolatedAsyncioTestCase):
    async def test_basic_200(self):
        body, status = await aggregate_openai_json(_agen([
            {"type": "delta", "text": "你"}, {"type": "delta", "text": "好"},
            {"type": "usage", "prompt_tokens": 2, "completion_tokens": 1, "model": "m"},
            {"type": "done", "message_id": "x", "session_id": "s"}]))
        self.assertEqual(status, 200)
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "你好")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["usage"]["total_tokens"], 3)

    async def test_error_503(self):
        body, status = await aggregate_openai_json(_agen([
            {"type": "error", "detail": "x", "code": "all_fallback_failed"}]))
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "all_fallback_failed")

    async def test_quota_exceeded_429(self):
        body, status = await aggregate_openai_json(_agen([
            {"type": "error", "detail": "配额超限", "code": "quota_exceeded",
             "http_status_hint": 429}]))
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "quota_exceeded")

    async def test_stream_reset_clears_content(self):
        body, _ = await aggregate_openai_json(_agen([
            {"type": "delta", "text": "废"}, {"type": "stream_reset"},
            {"type": "delta", "text": "终"},
            {"type": "done", "message_id": "x", "session_id": "s"}]))
        self.assertEqual(body["choices"][0]["message"]["content"], "终")


class CompatApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_compat_stream_chunks(self) -> None:
        r = self.client.post("/v1/chat?compat=openai", json={
            "messages": [{"role": "user", "content": "你好"}], "stream": True, "inject_context": False})
        self.assertEqual(r.status_code, 200)
        objs = _objs([f"data: {p}\n\n" for p in r.text.split("data: ") if p.strip()])
        self.assertTrue(all(o.get("object") == "chat.completion.chunk" for o in objs if "object" in o))
        self.assertTrue(r.text.rstrip().endswith("[DONE]"))

    def test_default_mode_still_mozi(self) -> None:
        r = self.client.post("/v1/chat", json={
            "messages": [{"role": "user", "content": "你好"}], "stream": True, "inject_context": False})
        self.assertIn('"type"', r.text, "默认无 compat 仍墨子 type= 事件")
        self.assertIn("delta", r.text)

    def test_compat_tools_400(self) -> None:
        r = self.client.post("/v1/chat?compat=openai", json={
            "messages": [{"role": "user", "content": "x"}], "tools": [{"type": "function"}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["param"], "tools")

    def test_compat_nonstream_json(self) -> None:
        r = self.client.post("/v1/chat?compat=openai", json={
            "messages": [{"role": "user", "content": "墨子用什么数据库？"}],
            "stream": False, "inject_context": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["object"], "chat.completion")
        self.assertTrue(r.json()["choices"][0]["message"]["content"], "非流式须有补全正文")


if __name__ == "__main__":
    unittest.main()
