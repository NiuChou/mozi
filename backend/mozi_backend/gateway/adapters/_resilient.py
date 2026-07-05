"""两 adapter 共用的韧性流式封装。

消解评审 MAJOR (超时语义解耦) + 采纳 stamina 幂等窗口重试:
- 只对 '建连 → raise_for_status' 幂等窗口重试; 一旦进入 aiter_lines 逐帧 yield, 绝不重试
  (首字节后整段重试会重复计费/重复 token)。emitted 标志是 mozi 必须自写的薄封装
  (没有库懂 LLM 流的'首字节前后'语义)。
- asyncio.timeout(UPSTREAM_READ_DEADLINE_S) 只包纯上游 aiter_lines 读取, 度量纯上游响应时长,
  与 orchestrator 侧 yield 给 SSE 消费者的下游回压解耦。
- stamina 缺失 → 纯 Python 退避兜底 (规则7: 零外呼/信创不破)。
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, Callable

import httpx

from .errors import is_retryable

try:
    import stamina
    _HAS_STAMINA = True
except ImportError:                       # 规则7: 缺库降级纯 Python 兜底
    _HAS_STAMINA = False

# 纯上游墙钟 (不含下游回压); orchestrator 另有更宽松的 END_TO_END_DEADLINE_S 端到端兜底
UPSTREAM_READ_DEADLINE_S = float(os.getenv("MOZI_UPSTREAM_READ_DEADLINE_S", "120"))
MAX_ATTEMPTS = int(os.getenv("MOZI_MODEL_RETRY", "2"))
RETRY_TOTAL_BUDGET_S = float(os.getenv("MOZI_RETRY_BUDGET_S", "20"))
RETRY_BASE_DELAY_S = float(os.getenv("MOZI_RETRY_BASE_S", "0.5"))

STOP = object()   # parse_line 返回此哨兵 → 停止读取 (如 OpenAI [DONE] 帧)


def _client_kwargs(transport: httpx.BaseTransport | None) -> dict:
    kw: dict = {"timeout": httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)}
    if transport is not None:
        kw["transport"] = transport       # transport 注入点正式化 (测试拦截/未来代理)
    return kw


async def stream_with_resilience(
    *, url: str, headers: dict, payload: dict,
    transport: httpx.BaseTransport | None,
    parse_line: Callable[[str], "str | object | None"],
    reset: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    """只对'建连→raise_for_status'幂等窗口重试; 进入读取后纯上游墙钟兜底, 绝不重试。

    reset: 每次 attempt 前 (含首次) 调用, 清 parse_line 的跨帧累积态。
    tool_call args 分片累积器不重置则重试会拼在首次半截上 → 坏 JSON。
    """

    async def _attempt() -> AsyncIterator[str]:
        if reset is not None:
            reset()                          # 每 attempt 起点清态, 防重试污染前次半截
        async with httpx.AsyncClient(**_client_kwargs(transport)) as client:
            # —— 幂等窗口起 ——
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                resp.raise_for_status()              # 4xx/5xx 在此抛 → 可被重试谓词判定
                # —— 幂等窗口止; 首帧后不再重试 ——
                # 纯上游墙钟: 只钳每次「读下一行」(await anext), yield 给消费者在 timeout 之外,
                # 故下游 SSE→前端回压不计入上游墙钟 (真正解耦; 否则慢消费者会误触发上游超时)。
                lines = resp.aiter_lines()
                while True:
                    try:
                        async with asyncio.timeout(UPSTREAM_READ_DEADLINE_S):
                            line = await anext(lines)
                    except StopAsyncIteration:
                        break
                    delta = parse_line(line)
                    if delta is STOP:
                        return
                    if delta:
                        yield delta                  # 下游回压在墙钟外

    if _HAS_STAMINA:
        async for d in _drive_with_stamina(_attempt):
            yield d
    else:
        async for d in _drive_with_fallback(_attempt):
            yield d


async def _drive_with_stamina(make_attempt) -> AsyncIterator[str]:
    last_exc: BaseException | None = None
    async for attempt in stamina.retry_context(
            on=is_retryable, attempts=MAX_ATTEMPTS, timeout=RETRY_TOTAL_BUDGET_S,
            wait_initial=RETRY_BASE_DELAY_S):
        with attempt:                     # stamina 按谓词决定是否再来一轮
            emitted = False
            try:
                async for d in make_attempt():
                    emitted = True
                    yield d
                return                    # 成功跑完 → 不再重试
            except BaseException as exc:
                if emitted:
                    raise                 # 首字节后: 绝不重试, 直接上抛给 orchestrator 降级
                last_exc = exc
                raise                     # 未发首字节: 交给 stamina 谓词判定是否重试
    if last_exc:
        raise last_exc


async def _drive_with_fallback(make_attempt) -> AsyncIterator[str]:   # 规则7 纯 Python 兜底
    for attempt in range(MAX_ATTEMPTS):
        emitted = False
        try:
            async for d in make_attempt():
                emitted = True
                yield d
            return
        except BaseException as exc:
            if emitted or not is_retryable(exc) or attempt + 1 >= MAX_ATTEMPTS:
                raise
            await asyncio.sleep(RETRY_BASE_DELAY_S * (2 ** attempt))   # 指数退避
