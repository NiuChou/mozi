"""适配层基类。所有适配器把各家差异收敛到统一流式 (§7.1 一键切换)。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

from ..models import ModelSpec

Message = dict[str, object]  # {"role": ..., "content": ...}; tool 角色 content 可为结构


@dataclass
class ToolCall:
    """模型请求的一次工具调用 (args 已解析为 dict)。"""

    id: str
    name: str
    args: dict = field(default_factory=dict)


@dataclass
class ToolCallsReady:
    """一回合的全部工具调用就绪 (parse_line 在 finish_reason=tool_calls 时产出)。

    astream 在 tools 非空时可能 yield 本事件 (与文本 str delta 混流);
    tools=None 时永不产出 → 旧纯 str 行为逐字不变 (向后兼容铁律)。
    """

    calls: list[ToolCall]


@dataclass
class ReasoningDelta:
    """推理模型 (如火山方舟 doubao-seed-evolving) 的思维链分片 (reasoning_content)。

    与正文 content 分流: 不计入答案正文, 仅供 UI『思考过程』展示。
    非推理模型永不产出 → 旧纯 str 行为逐字不变 (向后兼容铁律)。
    无此分流时, 推理模型在出正文前会长时间静默 (思维链阶段无 content), 前端气泡空白疑似卡死。
    """

    text: str


def estimate_tokens(text: str) -> int:
    """粗略 token 估算: 中文≈1字1token, 英文≈4字符1token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def compute_cost(spec: ModelSpec, tokens_in: int, tokens_out: int) -> float:
    """元 / 1k tokens 计价。"""
    return round((tokens_in * spec.price_in + tokens_out * spec.price_out) / 1000, 6)


class BaseAdapter:
    """子类实现 astream。

    usage: 可选的可变 dict (每次调用传入全新实例)。适配器在真实 API 回传用量时
    写入 {"tokens_in": int, "tokens_out": int}; 未写则上层回退估算。
    """

    name = "base"

    async def astream(self, spec: ModelSpec, messages: list[Message],
                      usage: dict | None = None, *,
                      tools: list[dict] | None = None,
                      transport: httpx.BaseTransport | None = None
                      ) -> AsyncIterator[str | ToolCallsReady]:
        """tools=None → 仅 yield 文本 str delta (旧行为)。
        tools 非空 → 模型请求工具时改 yield ToolCallsReady (与文本混流)。"""
        raise NotImplementedError
        yield ""  # pragma: no cover
