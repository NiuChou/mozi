"""EPIC-ADAPTER: is_retryable 错误分类 (纯函数)。"""
from __future__ import annotations

import asyncio
import unittest

import httpx

try:
    from ._helpers import TMP_DIR  # noqa: F401  触发临时库环境
except ImportError:
    from _helpers import TMP_DIR  # noqa: F401

from mozi_backend.gateway.adapters.errors import is_retryable  # noqa: E402
from mozi_backend.gateway.models import MODELS, build_fallback_chain  # noqa: E402


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://x/y")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


class IsRetryableTest(unittest.TestCase):
    def test_transient_network_and_timeout_retryable(self) -> None:
        self.assertTrue(is_retryable(httpx.ConnectError("x")))
        self.assertTrue(is_retryable(httpx.ReadTimeout("x")))
        self.assertTrue(is_retryable(asyncio.TimeoutError()))
        self.assertTrue(is_retryable(TimeoutError()))

    def test_5xx_429_408_retryable(self) -> None:
        for code in (408, 429, 500, 502, 503, 504):
            self.assertTrue(is_retryable(_status_error(code)), f"{code} 应可重试")

    def test_4xx_and_generic_not_retryable(self) -> None:
        for code in (400, 401, 403, 404):
            self.assertFalse(is_retryable(_status_error(code)), f"{code} 不应重试")
        self.assertFalse(is_retryable(ValueError("nope")))


class BuildFallbackChainTest(unittest.TestCase):
    def test_global_tail_when_not_domestic_only(self) -> None:
        chain = build_fallback_chain("deepseek-v4-pro", domestic_only=False)
        self.assertIn("claude", chain)
        self.assertIn("gpt", chain)
        self.assertEqual(chain[-1], "llama-local", "本地兜底始终殿后")
        self.assertEqual(chain.count("deepseek-v4-pro"), 1, "head 不重复")

    def test_domestic_only_excludes_global(self) -> None:
        chain = build_fallback_chain("glm-5.2", domestic_only=True)
        self.assertNotIn("claude", chain)
        self.assertNotIn("gpt", chain)
        self.assertTrue(all(MODELS[m].domestic for m in chain), "sovereign 链须全国产")


if __name__ == "__main__":
    unittest.main()
