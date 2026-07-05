"""墨子后端入口。Tauri 客户端经 IPC→本地 Rust 网关→本服务 (当前直连 HTTP)。

启动: 顺序化迁移 (schema) + 种子 (档位/demo 用户)。
挂载: UMA 网关 / Vault / Mozi-KG / Skill 兼容层。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends
from fastapi.middleware.cors import CORSMiddleware

try:
    from fastapi import FastAPI
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少 fastapi。请先 `pip install -r backend/requirements.txt`") from exc

from . import __version__
from .auth import current_user_id, generate_device_token
from .config import settings
from .db import dal
from .db.database import get_conn, init_db
from .db.seed import seed
from .gateway.api import router as gateway_router
from .skills.api import router as skills_router
from .sovereign import router as sovereign_router
from .telemetry import events
from .vault.api import router as vault_router

# CORS 白名单 (信创/本地优先): 仅放本机前端 (Vite/Tauri), 不用星号, 不带凭证
_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
]


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    generate_device_token()  # 启动生成/复用 device_token → data/.device_token
    import logging as _logging

    from .auth import _resolve_token_provider
    _logging.getLogger("mozi.auth").info("auth provider=%s", _resolve_token_provider().name)
    with get_conn() as conn:
        seed(conn)
        dal.ensure_user(conn, settings.default_user_id, settings.default_user_email, settings.default_region)
    events.capture("app_open", {"version": __version__, "mode": "local" if settings.local_first else "cloud"})
    yield


app = FastAPI(title="墨子 · UMA Gateway + Vault + Mozi-KG", version=__version__, lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

meta_router = APIRouter(tags=["meta"])


@meta_router.get("/health")
def health():
    return {"status": "ok", "version": __version__, "local_first": settings.local_first,
            "active_providers": settings.active_providers()}


@meta_router.get("/")
def root():
    return {"name": "墨子", "version": __version__,
            "docs": "/docs",
            "endpoints": ["/v1/chat", "/v1/vault/*", "/v1/kg/*", "/v1/skills/*",
                          "/v1/usage", "/v1/models", "/v1/events", "/v1/audit"]}


@meta_router.get("/v1/events")
def list_events(user_id: str = Depends(current_user_id), limit: int = 50):
    return {"events": events.recent(limit, user_id=user_id)}


@meta_router.get("/v1/audit")
def list_audit(user_id: str = Depends(current_user_id), limit: int = 50):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT action,resource,egress_flag,created_at FROM audit_log WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return {"audit": [dict(r) for r in rows]}


app.include_router(meta_router)
app.include_router(gateway_router)
app.include_router(vault_router)
app.include_router(skills_router)
app.include_router(sovereign_router)
