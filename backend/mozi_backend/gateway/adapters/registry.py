"""适配器选择: provider 在线走真实, 否则回退 mock (本地优先零外呼)。"""
from __future__ import annotations

from ...config import PROVIDERS
from ..models import ModelSpec
from .anthropic import AnthropicAdapter
from .base import BaseAdapter
from .mock import MockAdapter
from .openai_compat import OpenAICompatAdapter

_MOCK = MockAdapter()
_OPENAI = OpenAICompatAdapter()
_ANTHROPIC = AnthropicAdapter()


def select_adapter(spec: ModelSpec) -> tuple[BaseAdapter, bool]:
    """返回 (adapter, is_real)。is_real=False 表示走 mock。"""
    if spec.provider == "local":
        return _MOCK, False
    provider = PROVIDERS.get(spec.provider)
    if not provider or not provider.active:
        return _MOCK, False
    if provider.kind == "anthropic":
        return _ANTHROPIC, True
    return _OPENAI, True
