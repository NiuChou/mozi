"""UMA 网关 API: /v1/chat (流式) + 会话 + 计量 + 模型目录 + 路由预览。"""
from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import current_user_id
from ..config import settings
from ..db import dal
from ..db.database import get_conn
from ..schemas import ChatRequest, RoutePreviewRequest, SessionCreate, SessionUpdate
from ..util import jload
from . import quota
from .adapters.base import estimate_tokens
from .models import MODELS
from .openai_protocol import _openai_error, aggregate_openai_json, to_openai_sse
from .orchestrator import run_chat
from .router import RouteRequest, route

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

router = APIRouter(prefix="/v1", tags=["gateway"])


def _sse(event: dict, eid: int | None = None) -> str:
    prefix = f"id: {eid}\n" if eid is not None else ""
    return prefix + f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _run_chat_kwargs(req: ChatRequest, user_id: str, session_id: str, user_text: str, qd):
    return dict(user_id=user_id, session_id=session_id, user_text=user_text,
                policy=req.routing.policy, privacy_tier=req.routing.privacy_tier,
                budget_cny=req.routing.budget_cny, inject_context=req.inject_context,
                model_override=req.model, max_context=req.routing.max_context,
                active_providers=set(settings.active_providers()), quota_decision=qd)


def _wants_openai(request: Request, req: ChatRequest) -> bool:
    q = request.query_params.get("compat")
    if q:
        return q.lower() == "openai"
    return "application/openai" in request.headers.get("accept", "")


def _strict(request: Request) -> bool:
    return request.query_params.get("strict") == "1"


async def _raw_event_stream(req: ChatRequest, user_id: str, last_id: str | None,
                            user_text: str, est: int) -> AsyncIterator[dict]:
    """墨子原始事件 dict 流 (session + 续传回放 or run_chat)。供墨子/OpenAI 出站共用。"""
    with get_conn() as conn:
        dal.ensure_user(conn, user_id, f"{user_id}@mozi.local", settings.default_region)
        # 续传 (Last-Event-ID): session 属本人且末条 assistant → 回放 DB 尾态, 不触发 adapter
        session_id = req.session_id
        if last_id and session_id and dal.get_session(conn, user_id, session_id):
            msgs = dal.list_messages(conn, session_id)
            if msgs and msgs[-1]["role"] == "assistant":
                last = msgs[-1]
                rm = jload(last["routing_meta"])
                if rm:
                    yield {"type": "routing_metadata", **rm}
                rhits = jload(last["hits"], [])
                if rhits:
                    yield {"type": "retrieval", "injected": bool(last["injected"]),
                           "hits": rhits, "latency_ms": 0}
                yield {"type": "delta", "text": last["content_ref"] or ""}
                um = jload(last["usage_meta"])
                if um:
                    yield {"type": "usage", **um}
                yield {"type": "done", "message_id": last["message_id"], "session_id": session_id}
                return
        qd = quota.check_quota(conn, user_id, est_tokens=est, privacy_tier=req.routing.privacy_tier)
        if not session_id or not dal.get_session(conn, user_id, session_id):
            session_id = dal.create_session(conn, user_id, user_text[:24] or "新对话", req.routing.policy)
            yield {"type": "session", "session_id": session_id}
        async for event in run_chat(conn=conn, **_run_chat_kwargs(req, user_id, session_id, user_text, qd)):
            yield event


async def _mozi_sse(req: ChatRequest, user_id: str, last_id: str | None,
                    user_text: str, est: int) -> AsyncIterator[str]:
    seq = 0
    async for ev in _raw_event_stream(req, user_id, last_id, user_text, est):
        seq += 1
        yield _sse(ev, eid=seq)


@router.post("/chat")
async def chat(req: ChatRequest, request: Request, user_id: str = Depends(current_user_id)):
    last_id = request.headers.get("Last-Event-ID")
    user_text = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    est = estimate_tokens(user_text) + 512        # 保守上估, 仅作 degrade 软门
    compat = _wants_openai(request, req)

    if compat and req.tools:                      # 当前无 adapter 消费 tools → 声明即拒绝
        return JSONResponse(_openai_error("tools 暂不支持", "invalid_request_error",
                                          "unsupported_parameter", "tools"), status_code=400)

    if not req.stream:
        if compat:                                # OpenAI 非流式: 聚合 chat.completion (全降级失败 503)
            body, status = await aggregate_openai_json(
                _raw_event_stream(req, user_id, last_id, user_text, est))
            return JSONResponse(body, status_code=status)
        # 墨子非流式: 配额硬上限返真 HTTP 429 + {session_id, events}
        with get_conn() as conn:
            dal.ensure_user(conn, user_id, f"{user_id}@mozi.local", settings.default_region)
            qd = quota.check_quota(conn, user_id, est_tokens=est, privacy_tier=req.routing.privacy_tier)
            if qd.action == "block":
                return JSONResponse(status_code=429, content={
                    "code": "quota_exceeded", "detail": qd.reason, "remaining": qd.state.remaining})
            session_id = req.session_id
            if not session_id or not dal.get_session(conn, user_id, session_id):
                session_id = dal.create_session(conn, user_id, user_text[:24] or "新对话", req.routing.policy)
            events_out = [e async for e in run_chat(
                conn=conn, **_run_chat_kwargs(req, user_id, session_id, user_text, qd))]
        return JSONResponse({"session_id": session_id, "events": events_out})

    if compat:                                    # OpenAI 流式: chat.completion.chunk + [DONE]
        return StreamingResponse(
            to_openai_sse(_raw_event_stream(req, user_id, last_id, user_text, est), strict=_strict(request)),
            media_type="text/event-stream", headers=_SSE_HEADERS)
    return StreamingResponse(_mozi_sse(req, user_id, last_id, user_text, est),
                             media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/quota")
def quota_status(user_id: str = Depends(current_user_id)):
    with get_conn() as conn:
        st = quota.load_quota_state(conn, user_id)
    budget = st.token_budget
    return {"plan_code": st.plan_code, "token_budget": budget, "tokens_used": st.tokens_used,
            "remaining": st.remaining, "rate_multiplier": st.rate_multiplier, "period": st.period,
            "over_hard_cap": budget is not None and st.tokens_used >= budget * quota.HARD_CAP_RATIO}


@router.post("/sessions")
def create_session(body: SessionCreate, user_id: str = Depends(current_user_id)):
    with get_conn() as conn:
        dal.ensure_user(conn, user_id, f"{user_id}@mozi.local", settings.default_region)
        sid = dal.create_session(conn, user_id, body.title, body.model_policy)
    return {"session_id": sid, "title": body.title}


@router.get("/sessions")
def list_sessions(archived: bool = False, user_id: str = Depends(current_user_id)):
    """默认列未归档会话; ?archived=1 列已归档 (归档视图)。"""
    with get_conn() as conn:
        rows = dal.list_sessions(conn, user_id, archived=archived)
    return {"sessions": [dict(r) for r in rows]}


@router.patch("/sessions/{session_id}")
def update_session(session_id: str, body: SessionUpdate, user_id: str = Depends(current_user_id)):
    """重命名 (title) 和/或 归档切换 (archived)。会话不属本人 → 404。"""
    with get_conn() as conn:
        if not dal.get_session(conn, user_id, session_id):
            return JSONResponse({"error": "session not found"}, status_code=404)
        if body.title is not None:
            dal.rename_session(conn, user_id, session_id, body.title.strip() or "未命名")
        if body.archived is not None:
            dal.set_session_archived(conn, user_id, session_id, body.archived)
        row = dal.get_session(conn, user_id, session_id)
    return {"session": dict(row)}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, user_id: str = Depends(current_user_id)):
    """硬删会话 (级联清 messages)。会话不属本人 → 404。"""
    with get_conn() as conn:
        if not dal.delete_session(conn, user_id, session_id):
            return JSONResponse({"error": "session not found"}, status_code=404)
    return {"deleted": session_id}


@router.get("/sessions/{session_id}/messages")
def session_messages(session_id: str, user_id: str = Depends(current_user_id)):
    with get_conn() as conn:
        if not dal.get_session(conn, user_id, session_id):
            return JSONResponse({"error": "session not found"}, status_code=404)
        out = []
        for r in dal.list_messages(conn, session_id):
            d = dict(r)
            d["routing_meta"] = jload(d.get("routing_meta"))    # 旧消息 None
            d["hits"] = jload(d.get("hits"), [])                # 旧消息 []
            d["usage_meta"] = jload(d.get("usage_meta"))        # 旧消息 None
            d["injected"] = bool(d.get("injected"))             # 旧消息 False
            out.append(d)
    return {"messages": out}


@router.get("/usage")
def usage(user_id: str = Depends(current_user_id)):
    with get_conn() as conn:
        return dal.usage_summary(conn, user_id)


@router.get("/models")
def models(_user_id: str = Depends(current_user_id)):
    return {
        "active_providers": settings.active_providers(),
        "local_first": settings.local_first,
        "models": [
            {"id": m.id, "provider": m.provider, "context_window": m.context_window,
             "strengths": sorted(m.strengths), "domestic": m.domestic,
             "price_in": m.price_in, "price_out": m.price_out}
            for m in MODELS.values()
        ],
    }


@router.post("/route-preview")
def route_preview(body: RoutePreviewRequest, _user_id: str = Depends(current_user_id)):
    """不调模型, 只看路由决策 (Agent 控制室用)。"""
    decision = route(RouteRequest(policy=body.policy, privacy_tier=body.privacy_tier,
                                  est_tokens=body.est_tokens, budget_cny=body.budget_cny,
                                  text=body.text, active_providers=set(settings.active_providers())))
    return decision.to_metadata()
