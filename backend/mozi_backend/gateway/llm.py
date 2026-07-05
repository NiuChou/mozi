"""同步一次性 completion (KG 抽取等非流式场景用)。走既有适配层唯一出网路径。

零真实 provider → ('', False), 绝不出网。privacy_tier 透传 route: sovereign 时 domestic 硬过滤。
"""
from __future__ import annotations

import asyncio
import concurrent.futures

from .adapters.registry import select_adapter
from .models import get_model
from .router import RouteRequest, route


async def _collect(adapter, spec, messages) -> str:
    """coro 一次性消费: 每次调用新建。"""
    parts: list[str] = []
    async for d in adapter.astream(spec, messages, {}):
        parts.append(d)
    return "".join(parts)


def _run_coro_blocking(coro, timeout_s: float) -> str:
    """同步跑协程。单一超时来源 asyncio.wait_for; 在运行中事件循环内则起线程跑 asyncio.run。"""
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    def runner() -> str:
        return asyncio.run(asyncio.wait_for(coro, timeout_s))

    if not in_loop:
        return runner()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(runner).result()   # 无第二 timeout; wait_for 已护


def complete_sync(messages, *, policy: str = "economy", privacy_tier: str = "local_first",
                  active_providers: set[str], timeout_s: float = 20.0) -> tuple[str, bool]:
    """一次性补全, 返回 (text, is_real)。零真实 provider → ('', False)。"""
    if not active_providers:
        return "", False
    decision = route(RouteRequest(policy=policy, privacy_tier=privacy_tier,
                                  text=messages[-1]["content"], active_providers=active_providers))
    for model_id in decision.fallback_chain:
        spec = get_model(model_id)
        adapter, is_real = select_adapter(spec)
        if not is_real:
            continue                         # mock 跳过 → 上层回退正则
        try:
            text = _run_coro_blocking(_collect(adapter, spec, messages), timeout_s)
            if text.strip():
                return text, True
        except Exception:  # noqa: BLE001 — 降级
            continue
    return "", False
