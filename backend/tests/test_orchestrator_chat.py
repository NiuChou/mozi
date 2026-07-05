"""test-adequacy #1 #2 #4 + 契约B: 全流式编排 (run_chat) 直测。

#1 sovereign: privacy_tier=sovereign → chosen_model 及整条 fallback_chain 全 domestic=True,
   claude/gpt 不出现。
#2 egress 审计: mock chat → audit_log 含 vault.archive egress_flag=0 且无 egress=1;
   真实非本地 adapter → model.infer egress_flag=1。
#4 计量真实分支: 假适配器写 usage 且 is_real=True → metered=True 且 token 用真实值 (覆盖估算);
   mock → metered=False、cost=0。
契约B: 某次 astream 已 yield ≥1 delta 后抛异常降级 → 先 yield stream_reset。
"""
from __future__ import annotations

import unittest

try:  # 兼容 `discover -s tests` (顶层) 与 `unittest tests.X` (包) 两种运行方式
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

import httpx  # noqa: E402

import mozi_backend.gateway.orchestrator as orch  # noqa: E402
from mozi_backend.gateway.adapters import _resilient  # noqa: E402
from mozi_backend.gateway.adapters.base import compute_cost  # noqa: E402
from mozi_backend.gateway.adapters.mock import MockAdapter  # noqa: E402
from mozi_backend.gateway.adapters.openai_compat import OpenAICompatAdapter  # noqa: E402
from mozi_backend.gateway.models import get_model  # noqa: E402

try:
    from ._helpers import mock_async_client_factory, sse_body
except ImportError:
    from _helpers import mock_async_client_factory, sse_body

ALL_PROVIDERS = {"glm", "minimax", "kimi", "deepseek", "openai", "anthropic"}


async def _run(conn, **kw):
    defaults = dict(user_id="u_demo", session_id=None, user_text="墨子用什么数据库？",
                    active_providers=set())
    defaults.update(kw)
    # session 须存在
    from mozi_backend.db import dal
    dal.ensure_user(conn, defaults["user_id"], "x@mozi.local")
    if defaults["session_id"] is None:
        defaults["session_id"] = dal.create_session(conn, defaults["user_id"], "t", "auto")
    events = []
    async for e in orch.run_chat(conn=conn, **defaults):
        events.append(e)
    return events


class _FakeReal:
    """假真实适配器: 写 usage (真实 token) 模拟 include_usage 末帧。"""

    def __init__(self, tin: int, tout: int, text: str = "真实回答内容") -> None:
        self.tin, self.tout, self.text = tin, tout, text

    async def astream(self, spec, messages, usage=None):
        for ch in self.text:
            yield ch
        if usage is not None:
            usage["tokens_in"] = self.tin
            usage["tokens_out"] = self.tout


class _Boom:
    """yield 一个 delta 后抛错 (触发 stream_reset)。"""

    async def astream(self, spec, messages, usage=None):
        yield "半截"
        raise RuntimeError("forced mid-stream failure")


class SovereignRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_sovereign_chain_all_domestic(self) -> None:
        conn = fresh_conn()
        try:
            events = await _run(conn, privacy_tier="sovereign", active_providers=ALL_PROVIDERS,
                                inject_context=False)
        finally:
            conn.close()
        rm = next(e for e in events if e["type"] == "routing_metadata")
        chain = rm["fallback_chain"]
        self.assertTrue(chain, "fallback_chain 不应为空")
        for mid in [rm["chosen_model"], *chain]:
            self.assertTrue(get_model(mid).domestic, f"sovereign 链含非国产模型 {mid}")
        self.assertNotIn("claude", chain)
        self.assertNotIn("gpt", chain)
        self.assertNotIn("claude", [rm["chosen_model"]])
        self.assertNotIn("gpt", [rm["chosen_model"]])


class EgressAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_mock_chat_no_egress(self) -> None:
        conn = fresh_conn()
        try:
            await _run(conn, inject_context=False)  # 默认走 mock (active_providers 空)
            rows = conn.execute(
                "SELECT action,egress_flag FROM audit_log WHERE user_id='u_demo'").fetchall()
        finally:
            conn.close()
        actions = {r["action"]: r["egress_flag"] for r in rows}
        self.assertIn("vault.archive", actions, "归档须写审计")
        self.assertEqual(actions["vault.archive"], 0, "本地归档 egress 必须为 0")
        self.assertTrue(all(r["egress_flag"] == 0 for r in rows), "mock 链路不得出现 egress=1")
        self.assertNotIn("model.infer", actions, "mock 不应记 model.infer egress")

    async def test_real_nonlocal_adapter_egress_flagged(self) -> None:
        conn = fresh_conn()
        spec_seen = {}
        orig = orch.select_adapter

        def fake_select(spec):
            spec_seen["provider"] = spec.provider
            return _FakeReal(100, 50), True  # is_real=True

        orch.select_adapter = fake_select
        try:
            await _run(conn, inject_context=False, model_override="glm-5.2")  # glm.provider != local
            rows = conn.execute(
                "SELECT action,egress_flag FROM audit_log WHERE user_id='u_demo'").fetchall()
        finally:
            orch.select_adapter = orig
            conn.close()
        infer = [r for r in rows if r["action"] == "model.infer"]
        self.assertEqual(len(infer), 1, "真实非本地调用须记一条 model.infer")
        self.assertEqual(infer[0]["egress_flag"], 1, "真实非本地推理 egress 必须为 1")
        self.assertNotEqual(spec_seen["provider"], "local")


class MeteringTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_usage_metered_overrides_estimate(self) -> None:
        conn = fresh_conn()
        orig = orch.select_adapter
        orch.select_adapter = lambda spec: (_FakeReal(777, 333), True)
        try:
            events = await _run(conn, inject_context=False, model_override="glm-5.2")
        finally:
            orch.select_adapter = orig
            conn.close()
        usage = next(e for e in events if e["type"] == "usage")
        self.assertTrue(usage["metered"], "is_real + real_usage 须 metered=True")
        self.assertEqual(usage["prompt_tokens"], 777, "真实 token_in 须覆盖估算")
        self.assertEqual(usage["completion_tokens"], 333, "真实 token_out 须覆盖估算")
        expected = compute_cost(get_model("glm-5.2"), 777, 333)
        self.assertAlmostEqual(usage["cost_cny"], expected, places=6,
                               msg="cost_cny 须等于 compute_cost(真实 token)")
        self.assertGreater(usage["cost_cny"], 0.0)

    async def test_mock_not_metered_zero_cost(self) -> None:
        conn = fresh_conn()
        try:
            events = await _run(conn, inject_context=False)  # mock
        finally:
            conn.close()
        usage = next(e for e in events if e["type"] == "usage")
        self.assertFalse(usage["metered"], "mock 须 metered=False")
        self.assertEqual(usage["cost_cny"], 0.0, "mock 不计费")

    async def test_real_usage_zero_tokens_still_metered(self) -> None:
        # falsy-zero: 真实回传 0 token 仍算 metered (显式 in 判断, 不被 or 吞)
        conn = fresh_conn()
        orig = orch.select_adapter
        orch.select_adapter = lambda spec: (_FakeReal(0, 0, text="x"), True)
        try:
            events = await _run(conn, inject_context=False, model_override="glm-5.2")
        finally:
            orch.select_adapter = orig
            conn.close()
        usage = next(e for e in events if e["type"] == "usage")
        self.assertTrue(usage["metered"])
        self.assertEqual(usage["prompt_tokens"], 0)
        self.assertEqual(usage["completion_tokens"], 0)
        self.assertEqual(usage["cost_cny"], 0.0)


class StreamResetTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_reset_emitted_after_partial_delta(self) -> None:
        conn = fresh_conn()
        orig = orch.select_adapter
        from mozi_backend.gateway.adapters.mock import MockAdapter
        mock = MockAdapter()

        def fake_select(spec):
            if spec.id == "glm-5.2":
                return _Boom(), True
            return mock, False

        orch.select_adapter = fake_select
        try:
            events = await _run(conn, inject_context=False, model_override="glm-5.2")
        finally:
            orch.select_adapter = orig
            conn.close()
        types = [e["type"] for e in events]
        self.assertIn("stream_reset", types, "yield delta 后抛错降级须先发 stream_reset")
        self.assertIn("fallback", types, "stream_reset 后仍保留 fallback 事件")
        # 顺序: stream_reset 须在其后的 done 之前, 且在某个 delta 之后
        sr_idx = types.index("stream_reset")
        self.assertIn("delta", types[:sr_idx], "stream_reset 前须已有 delta")
        self.assertIn("done", types[sr_idx:], "stream_reset 后链路须继续完成")


def _delta_frame(content: str) -> str:
    return '{"choices":[{"index":0,"delta":{"content":"%s"}}]}' % content


class AdapterOrchestratorRetryTest(unittest.IsolatedAsyncioTestCase):
    """消解 MAJOR: adapter 内重试 ↔ orchestrator fallback 全链交互。

    真实 OpenAICompatAdapter 经 _resilient 重试; 仅 glm-5.2 为真 (注入 MockTransport),
    其余降级链模型走 mock (零外呼成功), 验证三场景。
    """

    def setUp(self) -> None:
        self._sa = orch.select_adapter
        self._cli = _resilient.httpx.AsyncClient
        self._cap = orch.events.capture
        self._base = _resilient.RETRY_BASE_DELAY_S
        _resilient.RETRY_BASE_DELAY_S = 0.0
        self.captured: list[dict] = []
        orch.events.capture = lambda ev, props=None, user_id=None: self.captured.append(
            {"ev": ev, "props": props or {}})
        # glm-5.2 → 真实 resilient adapter; 其余 → mock (降级链兜底成功)
        real = OpenAICompatAdapter()
        mock = MockAdapter()
        orch.select_adapter = lambda spec: (real, True) if spec.id == "glm-5.2" else (mock, False)

    def tearDown(self) -> None:
        orch.select_adapter = self._sa
        _resilient.httpx.AsyncClient = self._cli
        orch.events.capture = self._cap
        _resilient.RETRY_BASE_DELAY_S = self._base

    def _install(self, handler) -> dict:
        calls = {"n": 0}

        def counting(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return handler(calls["n"], req)

        _resilient.httpx.AsyncClient = mock_async_client_factory(httpx.MockTransport(counting))
        return calls

    def _errors(self) -> list[dict]:
        return [c["props"] for c in self.captured
                if c["ev"] == "error" and c["props"].get("type") == "model_call"]

    async def test_transient_retried_no_fallback(self) -> None:
        # 首次 500 (可重试) → adapter 内重试成功 → 不 fallback、无 model_call error
        def h(n, req):
            if n == 1:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, text=sse_body(_delta_frame("好"), "[DONE]"),
                                  headers={"content-type": "text/event-stream"})
        calls = self._install(h)
        conn = fresh_conn()
        try:
            events = await _run(conn, model_override="glm-5.2", inject_context=False)
        finally:
            conn.close()
        types = [e["type"] for e in events]
        self.assertEqual(calls["n"], 2, "首字节前 500 应 adapter 内重试一次")
        self.assertNotIn("fallback", types, "adapter 重试消化后不应触发 orchestrator fallback")
        self.assertIn("done", types)
        self.assertEqual(self._errors(), [], "重试消化 → 无 model_call error 事件")

    async def test_exhausted_500_falls_back_to_next(self) -> None:
        # 持续 500 → 耗尽 MAX_ATTEMPTS → adapter 上抛 → orchestrator fallback → mock 成功
        calls = self._install(lambda n, req: httpx.Response(503, text="down"))
        conn = fresh_conn()
        try:
            events = await _run(conn, model_override="glm-5.2", inject_context=False)
        finally:
            conn.close()
        types = [e["type"] for e in events]
        self.assertEqual(calls["n"], _resilient.MAX_ATTEMPTS, "可重试错误应耗尽 MAX_ATTEMPTS 次")
        self.assertIn("fallback", types, "耗尽后须 fallback 到下一模型")
        self.assertIn("done", types, "下一模型 (mock) 须成功完成")
        errs = self._errors()
        self.assertTrue(errs and errs[0]["retryable"] is True, "503 model_call error retryable=True")

    async def test_non_retryable_401_immediate_fallback(self) -> None:
        # 401 不可重试 → adapter 不重试立即上抛 → orchestrator 立即 fallback, retryable=False
        calls = self._install(lambda n, req: httpx.Response(401, text="unauthorized"))
        conn = fresh_conn()
        try:
            events = await _run(conn, model_override="glm-5.2", inject_context=False)
        finally:
            conn.close()
        types = [e["type"] for e in events]
        self.assertEqual(calls["n"], 1, "401 不可重试: adapter 只建连一次立即上抛")
        self.assertIn("fallback", types)
        self.assertIn("done", types)
        errs = self._errors()
        self.assertTrue(errs and errs[0]["retryable"] is False, "401 model_call error retryable=False")


if __name__ == "__main__":
    unittest.main()
