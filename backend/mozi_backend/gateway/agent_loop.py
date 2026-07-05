"""有界 agentic 工具循环引擎 (P0-A 阶段2)。

让模型自主决定调哪个工具、迭代 observe→act, 直到出终答或撞 max_steps。
每步穿 mozi 已有 choke 守卫: sovereign 出网硬门 (egress.classify, 调模型前拦截) /
allowed-tools 沙箱 (enforce_allowed) / 出网审计 (egress.audit model.infer; 工具自审计其出网) /
步间硬上限 (quota.over_hard_cap)。

P0-A v1 范围 (显式记录):
- 循环内单模型, 无降级链 (降级只在 loop 入口由调用方决定); 写工具仅在白名单内才执行。
- 不在循环内计费 model_calls, 仅记 agent_steps 轨迹 (token/工具/延迟/出网)。
- 终态 done / 聚合 usage 事件由包裹层 (orchestrator/skill, PR3) 发出; 本引擎只产
  delta/tool_call/tool_result/tool_denied/step/step_limit/egress_denied 子事件。
- spec.supports_tools=False → 退单发 (一次纯文本流, 不喂 tools)。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import AsyncIterator

from ..db import dal
from ..skills import tools
from ..telemetry import events
from . import egress, quota
from .adapters.base import ReasoningDelta, ToolCallsReady
from .adapters.registry import select_adapter
from .models import ModelSpec

DEFAULT_MAX_STEPS = int(os.getenv("MOZI_AGENT_MAX_STEPS", "4"))


def _usage_tokens(usage: dict) -> tuple[int, int]:
    return usage.get("tokens_in", 0) or 0, usage.get("tokens_out", 0) or 0


def _args_hash(calls) -> str:
    """本回合工具调用入参指纹 (供轨迹去重/审计, 不落明文 args)。"""
    blob = json.dumps([{"n": c.name, "a": c.args} for c in calls], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


async def run_tool_loop(
    conn,
    *,
    user_id: str,
    convo: list[dict],
    allowed_tools: list[str],
    spec: ModelSpec,
    run_id: str,
    privacy_tier: str = "local_first",
    parent_message_id: str | None = None,
    max_steps: int | None = None,
) -> AsyncIterator[dict]:
    """驱动有界工具循环, 流式 yield 事件。convo 会被就地追加工具观测 (调用方应传可变副本)。"""
    adapter, is_real = select_adapter(spec)

    # sovereign 出网硬门: 调模型前拦截 (egress 唯一判定源), 非国产推理直接拒, 不出网
    verdict = egress.classify(provider=spec.provider, privacy_tier=privacy_tier, is_real=is_real)
    if not verdict.allowed:
        events.capture("egress_denied", {"provider": spec.provider, "reason": verdict.reason,
                                         "run_id": run_id}, user_id)
        yield {"type": "egress_denied", "provider": spec.provider, "reason": verdict.reason}
        return

    # 不支持 function-calling (本地 mock 等) → 退单发: 一次纯文本流
    if not spec.supports_tools:
        usage: dict = {}
        async for delta in adapter.astream(spec, convo, usage):
            if isinstance(delta, str):
                yield {"type": "delta", "text": delta, "step": 0}
            elif isinstance(delta, ReasoningDelta):
                yield {"type": "reasoning", "text": delta.text, "step": 0}
        egress.audit(conn, user_id=user_id, provider=spec.provider, action="model.infer",
                     resource=spec.id, privacy_tier=privacy_tier, is_real=is_real)
        yield {"type": "step", "kind": "final", "step": 0}
        return

    schema = tools.tools_schema(allowed_tools)
    steps = max_steps if max_steps is not None else DEFAULT_MAX_STEPS

    for step in range(steps):
        usage = {}
        pending: ToolCallsReady | None = None
        text_parts: list[str] = []
        t0 = time.perf_counter()
        # 不 break: 排空生成器, 让末尾 usage 帧被解析 + 连接干净关闭 ([DONE]→STOP 即停)
        async for ev in adapter.astream(spec, convo, usage, tools=schema):
            if isinstance(ev, ToolCallsReady):
                pending = ev
                continue
            if isinstance(ev, ReasoningDelta):   # 推理模型思维链: 单独成事件, 不进终答正文
                yield {"type": "reasoning", "text": ev.text, "step": step}
                continue
            if pending is None:                  # 终答文本; tool 决定后的尾随文本忽略
                text_parts.append(ev)
                yield {"type": "delta", "text": ev, "step": step}
        latency_ms = int((time.perf_counter() - t0) * 1000)
        tok_in, tok_out = _usage_tokens(usage)

        # 模型推理出网审计 (mock/local is_real=False 不写行; sovereign 非国产已在入口拦截)
        egress.audit(conn, user_id=user_id, provider=spec.provider, action="model.infer",
                     resource=spec.id, privacy_tier=privacy_tier, is_real=is_real)

        if pending is None:                      # 模型出终答 → 收尾
            dal.record_agent_step(conn, run_id=run_id, user_id=user_id, step_idx=step,
                                  tool=None, parent_message_id=parent_message_id,
                                  tokens_in=tok_in, tokens_out=tok_out, latency_ms=latency_ms,
                                  egress=is_real, status="ok")
            yield {"type": "step", "kind": "final", "step": step}
            return

        # 回灌一条 OpenAI 合法 assistant 消息 (携本回合全部 tool_calls), 供真实 provider 下一步消费
        convo.append({
            "role": "assistant",
            "content": "".join(text_parts),
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}}
                           for c in pending.calls],
        })

        step_status = "ok"
        for call in pending.calls:
            yield {"type": "tool_call", "name": call.name, "args": call.args, "step": step}
            if not tools.enforce_allowed(call.name, allowed_tools):
                obs: dict = {"error": "tool_denied", "tool": call.name}
                events.capture("skill_error", {"type": "tool_denied", "tool": call.name}, user_id)
                yield {"type": "tool_denied", "name": call.name, "args": call.args,
                       "reason": "not_allowed", "step": step}
            else:
                try:
                    obs = tools.execute_call(call.name, conn, user_id, call.args,
                                             privacy_tier=privacy_tier)
                except Exception as exc:  # noqa: BLE001 — 工具失败当观测回灌让模型自纠, 不崩循环
                    obs = {"error": "tool_error", "tool": call.name, "detail": str(exc)[:200]}
                    step_status = "tool_error"
                    events.capture("skill_error", {"type": "tool_error", "tool": call.name}, user_id)
                yield {"type": "tool_result", "name": call.name, "result": obs, "step": step}
            # OpenAI 合法 tool 观测消息 (tool_call_id 对齐 assistant.tool_calls[].id)
            convo.append({"role": "tool", "tool_call_id": call.id,
                          "content": json.dumps(obs, ensure_ascii=False)})

        dal.record_agent_step(conn, run_id=run_id, user_id=user_id, step_idx=step,
                              tool=",".join(c.name for c in pending.calls),
                              parent_message_id=parent_message_id, args_hash=_args_hash(pending.calls),
                              tokens_in=tok_in, tokens_out=tok_out, latency_ms=latency_ms,
                              egress=is_real, status=step_status)

        # 步间硬上限护栏: 真实已用量超档位硬上限 → 提前止 (下次 check_quota 即 block)
        if quota.over_hard_cap(conn, user_id):
            events.capture("quota_over_hard_cap", {"run_id": run_id, "step": step}, user_id)
            yield {"type": "step_limit", "reason": "quota", "step": step, "max": steps}
            return

    yield {"type": "step_limit", "reason": "max_steps", "step": steps - 1, "max": steps}
