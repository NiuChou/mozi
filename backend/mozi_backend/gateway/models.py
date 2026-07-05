"""模型目录。能力标签驱动路由 (§8.1 三层: 应用→策略→适配)。

price_in/out: 元 / 1k tokens (粗略, 用于 Cost-Aware 估算)。
domestic: 信创 A 级 (国产, 可在 sovereign 隐私级使用)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    context_window: int
    strengths: frozenset[str]   # general/code/multimodal/long/chinese/reasoning
    price_in: float
    price_out: float
    domestic: bool
    max_output: int = 4096      # 末尾默认值, 现有位置参数构造全兼容; 驱动 adapter max_tokens
    supports_tools: bool = True  # function-calling 能力; False → agentic 循环退单发 (本地 mock 无 tool-call)


# 国产优先 + 全球版。对齐 §4 模型表 + 图9 路由。
MODELS: dict[str, ModelSpec] = {
    "glm-5.2": ModelSpec("glm-5.2", "glm", 256_000,
                         frozenset({"general", "chinese", "reasoning"}), 0.005, 0.015, True),
    "minimax-m3": ModelSpec("minimax-m3", "minimax", 1_000_000,
                            frozenset({"general", "multimodal", "long", "chinese"}), 0.008, 0.024, True),
    "kimi-k2.7-code": ModelSpec("kimi-k2.7-code", "kimi", 256_000,
                                frozenset({"code", "chinese"}), 0.012, 0.036, True),
    "deepseek-v4": ModelSpec("deepseek-v4", "deepseek", 128_000,
                             frozenset({"general", "reasoning", "code", "chinese"}), 0.002, 0.008, True),
    "deepseek-v4-flash": ModelSpec("deepseek-v4-flash", "deepseek", 128_000,
                                   frozenset({"general", "chinese"}), 0.0005, 0.0015, True),
    # 火山方舟 (Volcengine Ark) 托管 DeepSeek-V4-Pro · 信创国产 A 级 · 1M 上下文 · OpenAI 兼容
    "deepseek-v4-pro": ModelSpec("deepseek-v4-pro", "ark", 1_000_000,
                                 frozenset({"general", "reasoning", "code", "chinese", "long"}), 0.004, 0.012, True),
    # 火山方舟豆包 Seed 推理模型 · 信创国产 A 级 · 原生多模态 (image_url) + 深度推理 · OpenAI 兼容 /chat/completions
    "doubao-seed-evolving": ModelSpec("doubao-seed-evolving", "ark", 256_000,
                                      frozenset({"general", "reasoning", "multimodal", "chinese"}),
                                      0.0008, 0.008, True, max_output=8192),
    "claude": ModelSpec("claude", "anthropic", 200_000,
                        frozenset({"general", "code", "reasoning"}), 0.02, 0.08, False, max_output=8192),
    "gpt": ModelSpec("gpt", "openai", 128_000,
                     frozenset({"general", "code", "multimodal"}), 0.018, 0.06, False),
    "llama-local": ModelSpec("llama-local", "local", 32_000,
                             frozenset({"general", "chinese"}), 0.0, 0.0, True,
                             supports_tools=False),
}

# 各 provider 的真实 API model 名 (适配层映射)
PROVIDER_MODEL_NAME: dict[str, str] = {
    "glm-5.2": "glm-4-plus",
    "minimax-m3": "abab6.5s-chat",
    "kimi-k2.7-code": "moonshot-v1-128k",
    "deepseek-v4": "deepseek-chat",
    "deepseek-v4-flash": "deepseek-chat",
    "deepseek-v4-pro": "deepseek-v4-pro-260425",
    "doubao-seed-evolving": "doubao-seed-evolving",
    "claude": "claude-opus-4-8",
    "gpt": "gpt-4o",
}
# MiniMax 端点可配置 (消解 MINOR): 不在无 key 下硬编码赌单一模型名; 待持 key 的 reviewer 真机联调确认
PROVIDER_MODEL_NAME["minimax-m3"] = os.getenv("MOZI_MINIMAX_MODEL", "MiniMax-Text-01")

# 降级链 (图11 状态图): 主 → 高性价比 → 长上下文 → 代码 → 本地兜底 (全国产)
FALLBACK_CHAIN: list[str] = [
    "deepseek-v4-pro", "glm-5.2", "deepseek-v4-flash", "minimax-m3", "kimi-k2.7-code", "llama-local",
]
# 全球版兜底尾 (仅 domestic_only=False 即非 sovereign 时追加): 国产全挂时最后一搏
GLOBAL_FALLBACK_TAIL: list[str] = ["claude", "gpt"]


def build_fallback_chain(head: str, *, domestic_only: bool) -> list[str]:
    """以 head 起降级链, 接全局链去重 + 隐私硬过滤; llama-local 收尾。

    domestic_only (sovereign): 永不含 claude/gpt 等非国产 (信创硬过滤)。
    """
    chain = [head]
    for mid in FALLBACK_CHAIN + ([] if domestic_only else GLOBAL_FALLBACK_TAIL):
        if mid in chain:
            continue
        if domestic_only and not MODELS[mid].domestic:
            continue
        chain.append(mid)
    if "llama-local" in chain:                       # 本地兜底始终殿后
        chain = [m for m in chain if m != "llama-local"] + ["llama-local"]
    return chain


# litellm model 串映射 (ADAPTER 真实切换时消费; 本期仅数据, 零外呼)。
# 国产强制 openai/ 前缀 + 显式 api_base, 不依赖 litellm 内置 provider 别名 (防解析错配复发)。
_LITELLM_PREFIX = {"openai": "openai", "anthropic": "anthropic"}
LITELLM_MODEL: dict[str, str] = {}


def _build_litellm_map() -> None:
    from ..config import PROVIDERS
    for mid, spec in MODELS.items():
        if spec.provider == "local":
            continue
        api_model = PROVIDER_MODEL_NAME.get(mid, mid)
        kind = PROVIDERS[spec.provider].kind   # openai | anthropic
        LITELLM_MODEL[mid] = f"{_LITELLM_PREFIX[kind]}/{api_model}"


_build_litellm_map()


def get_model(model_id: str) -> ModelSpec:
    return MODELS.get(model_id, MODELS["glm-5.2"])
