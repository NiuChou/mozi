"""Mock 适配器: 零 key / 零外呼时的确定性流式回复 (本地优先, §9)。

让整条链路 (路由→检索注入→流式→归档→计量) 在无任何 API key 时也能端到端跑通。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from ..models import ModelSpec
from .base import BaseAdapter, Message, ToolCall, ToolCallsReady

# 测试在 user input 埋此标记触发确定性工具调用, 如 [[mock-tool:web_search]]
TOOL_TRIGGER = "[[mock-tool:"


class MockAdapter(BaseAdapter):
    name = "mock"

    async def astream(self, spec: ModelSpec, messages: list[Message],
                      usage: dict | None = None, *,
                      tools: list[dict] | None = None,
                      transport=None) -> AsyncIterator[str | ToolCallsReady]:
        # mock 不回传真实用量 → 上层估算 (本地占位); transport 形参仅为接口统一, 忽略
        _ = transport
        user_msg = str(next((m["content"] for m in reversed(messages) if m["role"] == "user"), ""))
        # 确定性 tool-call: tools 启用 + input 含 trigger + 尚无观测 → 产工具调用 (循环首回合)
        if tools and TOOL_TRIGGER in user_msg \
                and not any(m.get("role") == "tool" for m in messages):
            name = user_msg.split(TOOL_TRIGGER, 1)[1].split("]]", 1)[0].strip()
            yield ToolCallsReady([ToolCall("mock-1", name, {"query": "mock query"})])
            return
        # 否则逐字文本 (无 tools / 无 trigger / 已有观测 → 收尾): 旧行为逐字不变
        injected = any("【接地引用】" in str(m.get("content", "")) for m in messages)
        reply = self._compose(spec, user_msg, injected)
        for tok in self._chunks(reply):
            await asyncio.sleep(0.012)  # 模拟流式节奏
            yield tok

    @staticmethod
    def _compose(spec: ModelSpec, user_msg: str, injected: bool) -> str:
        head = f"[{spec.id} · mock] "
        snippet = (user_msg or "").strip().replace("\n", " ")
        if len(snippet) > 60:
            snippet = snippet[:60] + "…"
        grounding = "（已注入 Vault 接地引用，回答带出处）" if injected else "（未命中知识库，纯模型回答）"
        return (
            f"{head}收到问题：{snippet}\n\n"
            f"这是墨子的本地占位回复{grounding}。"
            f"UMA 网关已按策略选中 {spec.id}，流式逐字回放中。"
            f"配置任一模型 API key 后，本回复将由真实模型生成。"
        )

    @staticmethod
    def _chunks(text: str) -> list[str]:
        # 逐字 (中文) / 逐词片 (英文) 切, 贴近真实流式粒度
        out: list[str] = []
        buf = ""
        for ch in text:
            if "一" <= ch <= "鿿" or ch in "，。！？、\n":
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(ch)
            else:
                buf += ch
                if ch == " ":
                    out.append(buf)
                    buf = ""
        if buf:
            out.append(buf)
        return out
