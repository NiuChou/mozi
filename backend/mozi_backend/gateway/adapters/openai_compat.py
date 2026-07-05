"""OpenAI 兼容适配器 (GLM / DeepSeek / Kimi / MiniMax / OpenAI)。

各家均以 /chat/completions + SSE 暴露; 差异仅 base_url + model 名 (适配层屏蔽)。
仅当对应 env key 存在时激活, 否则上层回退 mock。出网即 §9 受控点。
重试退避/上游墙钟下沉到 _resilient.stream_with_resilience。
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from ...config import PROVIDERS
from ..models import PROVIDER_MODEL_NAME, ModelSpec
from ._resilient import STOP, stream_with_resilience
from .base import BaseAdapter, Message, ReasoningDelta, ToolCall, ToolCallsReady


def _safe_json(s: str) -> dict:
    """tool_call args 容错解析。坏 JSON → 空 dict (循环把错误当观测回灌让模型自纠, 不崩)。"""
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


class OpenAICompatAdapter(BaseAdapter):
    name = "openai_compat"

    async def astream(self, spec: ModelSpec, messages: list[Message],
                      usage: dict | None = None, *,
                      tools: list[dict] | None = None,
                      transport: httpx.BaseTransport | None = None
                      ) -> AsyncIterator[str | ToolCallsReady]:
        provider = PROVIDERS[spec.provider]
        api_model = PROVIDER_MODEL_NAME.get(spec.id, spec.id)
        url = f"{provider.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
        # include_usage: 末帧回传真实 prompt/completion tokens (精确计量)
        payload: dict = {"model": api_model, "messages": messages, "stream": True,
                         "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        acc: dict[int, dict] = {}   # index -> {id,name,args}; tool_call 跨帧分片累积

        def parse_line(line: str):
            if not line or not line.startswith("data:"):
                return None
            data = line[5:].strip()
            if data == "[DONE]":
                return STOP                       # 终止读取 (保留 [DONE] 断流语义)
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                return None
            u = obj.get("usage")
            if u and usage is not None:
                usage["tokens_in"] = u.get("prompt_tokens", 0)
                usage["tokens_out"] = u.get("completion_tokens", 0)
            choices = obj.get("choices") or []
            if not choices:
                return None
            choice = choices[0]
            delta = choice.get("delta", {}) or {}
            # tool_calls 分片累积 (id/name 早到, arguments 逐段拼)
            for tc in delta.get("tool_calls") or []:
                slot = acc.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
            # 完成信号: 一回合工具调用就绪
            if choice.get("finish_reason") == "tool_calls" and acc:
                calls = [ToolCall(s["id"], s["name"], _safe_json(s["args"]))
                         for _, s in sorted(acc.items())]
                return ToolCallsReady(calls)
            content = delta.get("content")
            if content:                                # 正文优先 (空串 falsy → 落到 None 被 resilient 滤掉)
                return content
            reasoning = delta.get("reasoning_content")  # 推理模型思维链分片 (doubao-seed 等)
            if reasoning:
                return ReasoningDelta(reasoning)
            return None

        # reset=acc.clear: 每 attempt 起点清累积器, 防重试拼在前次半截 args 上 → 坏 JSON
        async for d in stream_with_resilience(url=url, headers=headers, payload=payload,
                                              transport=transport, parse_line=parse_line,
                                              reset=acc.clear):
            yield d
