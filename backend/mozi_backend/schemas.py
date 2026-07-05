"""API 契约 (Pydantic)。对齐 §7 核心接口契约。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str


class RoutingOptions(BaseModel):
    policy: str = "auto"                 # 6 预设之一 或 auto (§7.1)
    budget_cny: float | None = None      # 单次成本上限 (Cost-Aware)
    privacy_tier: str = "local_first"    # local_first / cloud / sovereign
    max_context: int = 200_000


class ChatRequest(BaseModel):
    """POST /v1/chat — OpenAI 兼容超集 (§7.1)。"""
    messages: list[ChatMessage]
    stream: bool = True
    session_id: str | None = None
    routing: RoutingOptions = Field(default_factory=RoutingOptions)
    tools: list = Field(default_factory=list)
    inject_context: bool = True
    model: str | None = None             # 手动指定 → strategy=manual


class SessionCreate(BaseModel):
    title: str = "新对话"
    model_policy: str = "auto"


class SessionUpdate(BaseModel):
    """会话部分更新: 重命名 (title) 和/或 归档切换 (archived)。两者皆 None 即无操作。"""
    title: str | None = None
    archived: bool | None = None


class ArchiveRequest(BaseModel):
    """POST /v1/vault/archive。"""
    title: str
    content: str
    type: str = "笔记"
    storage_mode: str = "local"


class VaultSearchRequest(BaseModel):
    """POST /v1/vault/search。"""
    query: str
    k: int = 5
    routes: list[str] = Field(default_factory=lambda: ["bm25", "dense"])


class KGQueryRequest(BaseModel):
    """POST /v1/kg/query。"""
    entity: str
    hops: int = 1                        # 服务端钳 [1,5]
    max_nodes: int = 200                 # N-hop 子图节点预算 (防爆图)
    max_edges: int = 400                 # 边预算


class RoutePreviewRequest(BaseModel):
    text: str
    policy: str = "auto"
    privacy_tier: str = "local_first"
    budget_cny: float | None = None
    est_tokens: int = 1000


class SkillLoadRequest(BaseModel):
    skill_id: str
    level: int = 2                       # ②激活级: 载入正文


class SkillInvokeRequest(BaseModel):
    skill_id: str
    session_id: str | None = None
    input: str = ""
    auto: bool = True
    confirm: bool = False                # scan_status=warn 的 skill 需显式确认放行
