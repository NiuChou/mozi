"""墨子事件流 → OpenAI 兼容出站契约 (chat.completion.chunk SSE / chat.completion JSON)。

只在出站层转译, 不改 run_chat 事件产出。修复:
- all_fallback_failed: 流式发 data:{"error":{...}} 不发 finish_reason=stop; 非流式返 503 (不伪装空补全)。
- stream_reset 污染: per-attempt pending 缓冲, done 才 flush; stream_reset 丢弃作废半截。
缺 openai 库时降级手写 dict (零外呼离线可运行, 规则7)。命名 openai_protocol 避免与 adapters/openai_compat 撞名。
"""
from __future__ import annotations

import json
import time
from typing import AsyncIterator
from uuid import uuid4

try:
    from openai.types.chat import ChatCompletionChunk, CompletionUsage
    from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
    from openai.types.chat.chat_completion_chunk import ChoiceDelta
    _HAVE_OPENAI_TYPES = True
except Exception:  # noqa: BLE001 — 缺包 → 手写 dict 兜底
    _HAVE_OPENAI_TYPES = False


def new_chunk_id() -> str:
    return "chatcmpl-" + uuid4().hex


def _chunk(cid: str, model: str, created: int, choices: list[dict], usage: dict | None = None) -> dict:
    if _HAVE_OPENAI_TYPES:
        obj = ChatCompletionChunk(
            id=cid, object="chat.completion.chunk", created=created, model=model,
            choices=[ChunkChoice(index=c["index"], delta=ChoiceDelta(**c["delta"]),
                                 finish_reason=c["finish_reason"]) for c in choices],
            usage=CompletionUsage(**usage) if usage else None)
        return obj.model_dump(exclude_none=True)
    d: dict = {"id": cid, "object": "chat.completion.chunk", "created": created,
               "model": model, "choices": choices}
    if usage:
        d["usage"] = usage
    return d


def _xmozi(cid: str, model: str, created: int, ev: dict) -> dict:
    d = _chunk(cid, model, created, [{"index": 0, "delta": {}, "finish_reason": None}])
    d["x_mozi"] = ev
    return d


def _sse_raw(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _openai_error(msg: str, etype: str, code: str, param: str | None = None) -> dict:
    return {"error": {"message": msg, "type": etype, "param": param, "code": code}}


async def to_openai_sse(events: AsyncIterator[dict], *, strict: bool = False) -> AsyncIterator[str]:
    """墨子事件流 → OpenAI chat.completion.chunk SSE 行 (含 [DONE])。"""
    cid = new_chunk_id()
    created = int(time.time())
    model = "auto"
    pending: list[str] = []
    role_sent = False
    final_usage: dict | None = None

    def _content_chunk(text: str, *, with_role: bool) -> dict:
        delta = {"role": "assistant", "content": text} if with_role else {"content": text}
        return _chunk(cid, model, created, [{"index": 0, "delta": delta, "finish_reason": None}])

    async for ev in events:
        t = ev.get("type")
        if t == "routing_metadata":
            model = ev.get("chosen_model", model)
        elif t == "delta":
            pending.append(ev.get("text", ""))            # 不立即 yield (per-attempt 缓冲)
        elif t == "stream_reset":
            pending.clear()                               # 丢弃作废 token (修复污染)
            if not strict:
                yield _sse_raw(_xmozi(cid, model, created, ev))
        elif t == "usage":
            model = ev.get("model", model)                # final_model 回填
            tin, tout = ev.get("prompt_tokens", 0), ev.get("completion_tokens", 0)
            final_usage = {"prompt_tokens": tin, "completion_tokens": tout,
                           "total_tokens": tin + tout}
        elif t == "done":
            if pending:
                yield _sse_raw(_content_chunk("".join(pending), with_role=not role_sent))
                role_sent = True
                pending.clear()
            yield _sse_raw(_chunk(cid, model, created,
                                  [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                  usage=final_usage))
            yield "data: [DONE]\n\n"
            return
        elif t == "error":                                # 全降级失败: 不发 stop
            pending.clear()
            code = ev.get("code", "all_fallback_failed")
            yield _sse_raw(_openai_error(ev.get("detail", "all fallback failed"), "api_error", code))
            yield "data: [DONE]\n\n"
            return
        elif not strict:                                  # 其他超集事件 → x_mozi
            yield _sse_raw(_xmozi(cid, model, created, ev))


async def aggregate_openai_json(events: AsyncIterator[dict]) -> tuple[dict, int]:
    """墨子事件流 → OpenAI chat.completion 单对象 + HTTP 状态码 (全降级失败 503)。"""
    cid = new_chunk_id()
    created = int(time.time())
    model = "auto"
    content_parts: list[str] = []
    usage: dict | None = None
    errored: dict | None = None
    async for ev in events:
        t = ev.get("type")
        if t == "routing_metadata":
            model = ev.get("chosen_model", model)
        elif t == "delta":
            content_parts.append(ev.get("text", ""))
        elif t == "stream_reset":
            content_parts.clear()                         # 清空作废半截
        elif t == "usage":
            model = ev.get("model", model)
            tin, tout = ev.get("prompt_tokens", 0), ev.get("completion_tokens", 0)
            usage = {"prompt_tokens": tin, "completion_tokens": tout, "total_tokens": tin + tout}
        elif t == "error":
            errored = ev
    if errored is not None:
        code = errored.get("code", "all_fallback_failed")
        status = errored.get("http_status_hint", 503)  # 配额超限 → 429, 其余降级失败 → 503
        return _openai_error(errored.get("detail", "all fallback failed"), "api_error", code), status
    body = {"id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "".join(content_parts)},
                         "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
    return body, 200
