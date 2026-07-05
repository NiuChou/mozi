"""Anthropic 适配器 (Claude)。Messages API + SSE; system 抽出, 差异收敛到统一流式。

max_tokens 由 ModelSpec.max_output 驱动 (消解写死 2048); 韧性下沉到 _resilient。
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from ...config import PROVIDERS
from ..models import PROVIDER_MODEL_NAME, ModelSpec
from ._resilient import stream_with_resilience
from .base import BaseAdapter, Message


class AnthropicAdapter(BaseAdapter):
    name = "anthropic"

    async def astream(self, spec: ModelSpec, messages: list[Message],
                      usage: dict | None = None, *,
                      tools: list[dict] | None = None,
                      transport: httpx.BaseTransport | None = None) -> AsyncIterator[str]:
        _ = tools  # Anthropic function-calling 尚未接线 (TODO): 收下并忽略, 仍仅 yield str; agentic 循环回退纯文本
        provider = PROVIDERS[spec.provider]
        api_model = PROVIDER_MODEL_NAME.get(spec.id, "claude-opus-4-8")
        system = " ".join(m["content"] for m in messages if m["role"] == "system") or None
        convo = [{"role": m["role"], "content": m["content"]}
                 for m in messages if m["role"] in ("user", "assistant")]
        url = f"{provider.base_url}/messages"
        headers = {
            "x-api-key": provider.api_key or "",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        max_out = getattr(spec, "max_output", 4096)   # max_tokens 由 spec 驱动, 不写死
        payload: dict = {"model": api_model, "messages": convo, "max_tokens": max_out, "stream": True}
        if system:
            payload["system"] = system

        def parse_line(line: str):
            if not line or not line.startswith("data:"):
                return None
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                return None
            etype = obj.get("type")
            if etype == "content_block_delta":
                return obj.get("delta", {}).get("text")
            if etype == "message_start" and usage is not None:
                usage["tokens_in"] = obj.get("message", {}).get("usage", {}).get("input_tokens", 0)
            elif etype == "message_delta" and usage is not None:
                out = obj.get("usage", {}).get("output_tokens")
                if out is not None:
                    usage["tokens_out"] = out
            return None

        async for d in stream_with_resilience(url=url, headers=headers, payload=payload,
                                              transport=transport, parse_line=parse_line):
            yield d
