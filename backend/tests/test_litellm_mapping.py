"""EPIC-METER (v3): LITELLM_MODEL 映射逐一回归 (ADAPTER 真实切换前锁死, 防解析错配复发)。"""
from __future__ import annotations

import unittest

try:
    from ._helpers import TMP_DIR  # noqa: F401  触发临时库环境
except ImportError:
    from _helpers import TMP_DIR  # noqa: F401

from mozi_backend.gateway.models import (  # noqa: E402
    LITELLM_MODEL,
    MODELS,
    PROVIDER_MODEL_NAME,
)


class LitellmMappingTest(unittest.TestCase):
    def test_known_mappings(self) -> None:
        expect = {
            "glm-5.2": "openai/glm-4-plus",
            "deepseek-v4-pro": "openai/deepseek-v4-pro-260425",
            "deepseek-v4": "openai/deepseek-chat",
            "kimi-k2.7-code": "openai/moonshot-v1-128k",
            "minimax-m3": "openai/MiniMax-Text-01",
            "claude": "anthropic/claude-opus-4-8",
            "gpt": "openai/gpt-4o",
        }
        for mid, lit in expect.items():
            self.assertEqual(LITELLM_MODEL[mid], lit, f"{mid} litellm 串错配")

    def test_local_excluded(self) -> None:
        self.assertNotIn("llama-local", LITELLM_MODEL, "local 终点不进 litellm 映射")

    def test_domestic_prefixes_constrained(self) -> None:
        for mid, lit in LITELLM_MODEL.items():
            self.assertTrue(lit.startswith(("openai/", "anthropic/")),
                            f"{mid} 前缀须显式 (不依赖 litellm 内置别名): {lit}")

    def test_provider_model_name_drift_guard(self) -> None:
        # litellm 串后缀须 == PROVIDER_MODEL_NAME (改名漂移立即被本断言捕获)
        for mid in LITELLM_MODEL:
            if MODELS[mid].provider == "local":
                continue
            suffix = LITELLM_MODEL[mid].split("/", 1)[1]
            self.assertEqual(suffix, PROVIDER_MODEL_NAME.get(mid, mid),
                             f"{mid}: litellm 后缀与 PROVIDER_MODEL_NAME 漂移")


if __name__ == "__main__":
    unittest.main()
