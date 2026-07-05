"""适配层错误分类: 区分可重试 (瞬时) 与不可重试 (确定性失败)。纯函数, 易测。

parse 思路 reference-only 借鉴 LiteLLM 的 per-error-type 分类, 但不引入 LiteLLM
(重依赖 + retry/fallback 交互 bug)。
"""
from __future__ import annotations

import asyncio

import httpx

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True                       # 连接/读写超时/连接重置
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True                       # asyncio.timeout 触发 (3.11+ 即内建 TimeoutError)
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS   # 429/5xx/408 可重试; 4xx 不可
    return False
