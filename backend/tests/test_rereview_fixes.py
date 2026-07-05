"""复评核验后修复的回归测试。

锁死 4 个由上轮修复引入/遗漏、经复评蜂群确认的缺陷:
1. /v1/events 鉴权 (require_auth=1 → 401) —— 上轮 auth 漏覆盖该端点。
2. /v1/events 跨用户隔离 (multiuser 下不读他人遥测)。
3. sovereign 信创硬过滤覆盖 model_override 路径 (手动指定非国产无效)。
4. skill invoke mock 不计费 (cost=0, metered=false), 与 orchestrator 口径对齐。
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import dataclasses  # noqa: E402

try:  # 兼容 `discover -s tests` (顶层) 与包内运行; import 触发 _helpers 的临时库/环境副作用
    from ._helpers import _auth_headers, parse_sse
except ImportError:
    from _helpers import _auth_headers, parse_sse

from mozi_backend import auth  # noqa: E402
from mozi_backend.gateway.models import MODELS  # noqa: E402
from mozi_backend.main import app  # noqa: E402


class _SettingsCtx:
    """临时替换 auth.settings 开关, 退出还原 (frozen dataclass 安全)。透传任意字段。"""

    def __init__(self, **ov) -> None:
        self.ov = ov

    def __enter__(self):
        self._orig = auth.settings
        auth.settings = dataclasses.replace(auth.settings, **self.ov)
        return auth.settings

    def __exit__(self, *a):
        auth.settings = self._orig


class EventsAuthTest(unittest.TestCase):
    def test_events_requires_auth_when_enabled(self) -> None:
        with TestClient(app) as client:  # lifespan 生成 device_token
            with _SettingsCtx(require_auth=True):
                r = client.get("/v1/events")  # 无 Bearer
                self.assertEqual(r.status_code, 401, "require_auth=1 下 /v1/events 须 401 (鉴权覆盖)")
                token = auth._DEVICE_TOKEN
                self.assertTrue(token, "lifespan 应已生成 device_token")
                ok = client.get("/v1/events", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(ok.status_code, 200, "正确 token 放行")

    def test_events_no_cross_user_leak(self) -> None:
        with TestClient(app) as client:
            with _SettingsCtx(multiuser=True):
                client.post("/v1/chat", json={"messages": [{"role": "user", "content": "alice 私密会话"}],
                                              "inject_context": False}, headers=_auth_headers("u_alice"))
                client.post("/v1/chat", json={"messages": [{"role": "user", "content": "bob 的会话"}],
                                              "inject_context": False}, headers=_auth_headers("u_bob"))
                evs = client.get("/v1/events", headers=_auth_headers("u_bob")).json()["events"]
                self.assertTrue(evs, "bob 应能看到自己的事件")
                self.assertTrue(all(e["user_id"] == "u_bob" for e in evs),
                                "bob 的 /v1/events 不得混入他人 (alice/默认) 事件")


class SovereignOverrideTest(unittest.TestCase):
    def test_model_override_cannot_bypass_sovereign(self) -> None:
        with TestClient(app) as client:
            r = client.post("/v1/chat", json={
                "messages": [{"role": "user", "content": "信创级强制非国产测试"}],
                "model": "claude", "inject_context": False,
                "routing": {"privacy_tier": "sovereign"}})
            evts = parse_sse(r.text)
            meta = next((e for e in evts if e["type"] == "routing_metadata"), {})
            chosen = meta.get("chosen_model")
            self.assertIsNotNone(chosen)
            self.assertTrue(MODELS[chosen].domestic,
                            f"sovereign 下 model_override=claude 仍选了非国产 {chosen} (信创硬过滤被绕过)")
            self.assertNotIn("claude", meta.get("fallback_chain", []))
            self.assertNotIn("gpt", meta.get("fallback_chain", []))
            self.assertTrue(all(MODELS[m].domestic for m in meta.get("fallback_chain", [])),
                            "sovereign 降级链须全国产")


class SkillMeteringTest(unittest.TestCase):
    def test_skill_invoke_mock_not_metered(self) -> None:
        with TestClient(app) as client:
            client.post("/v1/skills/discover")
            skills = client.get("/v1/skills").json()["skills"]
            demo = next((s for s in skills if s["name"] == "mozi-demo"), skills[0])
            inv = client.post("/v1/skills/invoke", json={
                "skill_id": demo["skill_id"], "input": "墨子用 SQLite。"}).json()
            self.assertEqual(inv.get("status"), "ok")
            self.assertEqual(inv.get("cost_cny"), 0.0, "mock 链路 skill 调用不应计费")
            self.assertIs(inv.get("metered"), False, "mock 链路 metered=False (与 orchestrator 口径一致)")


if __name__ == "__main__":
    unittest.main()
