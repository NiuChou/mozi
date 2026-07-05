"""Chat 编排: 对话—路由—知识 三主线闭环 (§8.4 图3)。

流程: 持久化 → (智能上下文注入: Vault 检索+标来源) → UMA 路由 → 流式 (失败按降级链) →
归档闭环 (Q+A 入 Vault + KG 回填) → 计量入账 model_calls。产出统一流式事件 (§7.1)。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from typing import AsyncIterator

from ..db import dal
from ..db.database import transaction
from ..telemetry import events
from ..vault import kg, retrieval, service
from . import egress, quota
from .adapters.base import ReasoningDelta, estimate_tokens
from .adapters.errors import is_retryable
from .adapters.registry import select_adapter
from .models import MODELS, build_fallback_chain, get_model
from .router import RouteRequest, route

GROUNDING_HEADER = "【接地引用】以下是从用户知识库检索到的相关资料，回答时请优先采用并标注来源：\n"
# 含下游 (SSE→前端) 回压的整段单次尝试上限, 非纯上游墙钟 (纯上游由 adapter 内 UPSTREAM_READ_DEADLINE_S 兜)
END_TO_END_DEADLINE_S = float(os.getenv("MOZI_END_TO_END_DEADLINE_S", "240"))


def _build_grounding(hits: list[retrieval.Hit]) -> str:
    lines = [GROUNDING_HEADER]
    for i, h in enumerate(hits, 1):
        lines.append(f"[{i}] (来源: {h.title}) {h.text}")
    return "\n".join(lines)


def _hits_for_persist(hits: list[retrieval.Hit]) -> list[dict]:
    """落库用: 去命中全文 text 防行膨胀。provenance 是 Hit.to_dict 计算值 (非字段),
    此处显式计算 (与 to_dict 一致), 不可读 h.provenance (会 AttributeError)。
    SSE/续传回放仍用 h.to_dict() 发完整 hits (含 text 供展示)。"""
    return [{"chunk_id": h.chunk_id, "doc_id": h.doc_id, "title": h.title,
             "score": round(h.score, 5), "provenance": f"{h.title} · {h.chunk_id}",
             "routes": h.routes} for h in hits]


def _trim_to_budget(convo: list[dict[str, str]], max_context: int) -> tuple[list[dict[str, str]], int]:
    """裁剪历史到 max_context (留 25% 输出余量)。system(接地引用) 恒保留, 余取最近。"""
    budget = max(2000, int(max_context * 0.75))
    system = [m for m in convo if m["role"] == "system"]
    rest = [m for m in convo if m["role"] != "system"]
    used = sum(estimate_tokens(m["content"]) for m in system)
    kept: list[dict[str, str]] = []
    for m in reversed(rest):
        t = estimate_tokens(m["content"])
        if kept and used + t > budget:
            break
        kept.append(m)
        used += t
    kept.reverse()
    return system + kept, used


async def run_chat(
    *,
    conn: sqlite3.Connection,
    user_id: str,
    session_id: str,
    user_text: str,
    policy: str = "auto",
    privacy_tier: str = "local_first",
    budget_cny: float | None = None,
    inject_context: bool = True,
    model_override: str | None = None,
    max_context: int = 200_000,
    active_providers: set[str],
    quota_decision: "quota.QuotaDecision | None" = None,
    tools: list | None = None,   # P0-2 预留: OpenAI 兼容 tools 透传 (本期 _tools_supported=False, 不消费)
) -> AsyncIterator[dict]:
    # 0. 配额硬上限 block: 不持久化/不计量/不归档 (api.py 已对非流式兜底 429, 此处流式防御)
    if quota_decision and quota_decision.action == "block":
        yield {"type": "error", "code": "quota_exceeded",
               "detail": quota_decision.reason, "http_status_hint": 429}
        return
    # 1. 持久化用户消息 (先取历史末态)。重连重跑场景幂等: 末条已是同文 user 则不重复插入
    history = dal.list_messages(conn, session_id)
    prev_model = next((m["model"] for m in reversed(history) if m["role"] == "assistant" and m["model"]), "auto")
    last = history[-1] if history else None
    if not (last and last["role"] == "user" and (last["content_ref"] or "") == user_text):
        dal.add_message(conn, session_id, "user", user_text)

    # 2. 装配对话历史
    history = dal.list_messages(conn, session_id)
    convo: list[dict[str, str]] = [{"role": m["role"], "content": m["content_ref"] or ""} for m in history]

    # 3. 智能上下文注入 (Vault 检索 → 接地引用)
    injected = False
    hits_persist: list[dict] = []      # 落库用 (去 text); SSE 仍发完整 hits
    if inject_context:
        res = retrieval.search(conn, user_id, user_text, k=4)
        hits_persist = _hits_for_persist(res.hits)
        dal.log_retrieval(conn, user_id=user_id, query=user_text,
                          routes=[h.chunk_id for h in res.hits], latency_ms=res.latency_ms,
                          injected=res.injected)
        events.capture("retrieval_query", {"hit_count": len(res.hits), "latency_ms": res.latency_ms,
                                           "injected": res.injected,
                                           "routes": sorted({r for h in res.hits for r in h.routes})}, user_id)
        if res.injected and res.hits:
            convo.insert(0, {"role": "system", "content": _build_grounding(res.hits)})
            injected = True
        yield {"type": "retrieval", "injected": injected,
               "hits": [h.to_dict() for h in res.hits], "latency_ms": res.latency_ms}

    # 3b. 上下文裁剪到 max_context (防长会话无界膨胀)
    convo, est_in = _trim_to_budget(convo, max_context)
    events.capture("chat_send", {"session_id": session_id, "model_policy": policy,
                                 "injected": injected}, user_id)

    # 4. UMA 路由
    # 信创硬过滤 (§9): sovereign 级下手动指定非国产模型无效 → 回落自动路由 (route 强制国产 A 级)
    sovereign = privacy_tier == "sovereign"
    override_ok = bool(model_override and model_override in MODELS
                       and not (sovereign and not MODELS[model_override].domestic))
    if override_ok:
        chain = build_fallback_chain(model_override, domestic_only=sovereign)
        decision_meta = {"type": "routing_metadata", "chosen_model": chain[0],
                         "strategy": "manual", "fallback_used": False, "fallback_chain": chain,
                         "privacy_tier": privacy_tier, "task_type": "manual",
                         "reason": "用户手动指定模型", "scores": {}}
        chosen_chain = chain
        strategy = "manual"
        if chain[0] != prev_model:
            events.capture("model_switch", {"from_model": prev_model, "to_model": chain[0]}, user_id)
    else:
        decision = route(RouteRequest(policy=policy, privacy_tier=privacy_tier, est_tokens=est_in,
                                      budget_cny=budget_cny, text=user_text,
                                      active_providers=active_providers))
        decision_meta = decision.to_metadata(fallback_used=False)
        chosen_chain = decision.fallback_chain
        strategy = decision.strategy
    yield decision_meta

    # 4b. 配额 degrade 统一替换点 (覆盖 manual+auto 两路): forced_model 置首 + 去重 + sovereign 过滤
    if quota_decision and quota_decision.action == "degrade" and quota_decision.forced_model:
        fm = quota_decision.forced_model
        assert MODELS[fm].domestic, "degrade forced_model 必须国产 (sovereign 双保险)"
        tail = [m for m in chosen_chain if m != fm]
        if sovereign:
            tail = [m for m in tail if MODELS[m].domestic]
        chosen_chain = [fm] + tail
        yield {"type": "quota_degrade", "forced_model": fm, "reason": quota_decision.reason}

    # 5. 流式 (失败按降级链 图11)。每次尝试夹 perf_counter 测延迟, 收真实用量
    answer = ""
    final_model = chosen_chain[0]
    fallback_used = False
    is_real = False
    real_usage: dict = {}
    latency_ms = 0
    succeeded = False
    for idx, model_id in enumerate(chosen_chain):
        spec = get_model(model_id)
        adapter, real = select_adapter(spec)
        attempt_usage: dict = {}
        attempt_answer = ""
        emitted_delta = False
        t0 = time.perf_counter()
        try:
            # 端到端兜底 deadline (含下游回压); 纯上游墙钟与重试退避已下沉 adapter
            async with asyncio.timeout(END_TO_END_DEADLINE_S):
                async for delta in adapter.astream(spec, convo, attempt_usage):
                    # 推理模型思维链分片: 单独成 reasoning 事件, 不计入答案正文、不触发 stream_reset 语义。
                    # 缺此分流时推理阶段无 content → 气泡长时间空白 (疑似卡死)。
                    if isinstance(delta, ReasoningDelta):
                        yield {"type": "reasoning", "text": delta.text}
                        continue
                    attempt_answer += delta
                    emitted_delta = True
                    yield {"type": "delta", "text": delta}
            answer = attempt_answer
            final_model, is_real, real_usage = model_id, real, attempt_usage
            latency_ms = int((time.perf_counter() - t0) * 1000)
            fallback_used = idx > 0
            succeeded = True
            break
        except Exception as exc:  # noqa: BLE001 — 降级容错 (含 adapter 重试已耗尽/不可重试/上游超时)
            fallback_used = True
            events.capture("error", {"type": "model_call", "module": "gateway", "model": model_id,
                                     "retryable": is_retryable(exc), "detail": str(exc)[:200]}, user_id)
            # 契约B: 本次尝试已 yield 过 delta 才抛错 → 先令前端清空当前气泡正文再续接
            if emitted_delta:
                yield {"type": "stream_reset", "from_model": model_id}
            yield {"type": "fallback", "from_model": model_id,
                   "to_model": chosen_chain[idx + 1] if idx + 1 < len(chosen_chain) else "n/a"}
            continue

    # 5b. 全降级失败 guard: 链上无任何 adapter 成功 → 不持久化/不计量/不归档
    if not succeeded:
        events.capture("error", {"type": "all_fallback_failed", "module": "gateway",
                                 "chain": chosen_chain}, user_id)
        yield {"type": "error", "detail": "所有模型降级均失败", "fallback_chain": chosen_chain}
        return

    if fallback_used:
        yield {"type": "routing_metadata", "chosen_model": final_model, "strategy": strategy,
               "fallback_used": True, "fallback_chain": chosen_chain, "privacy_tier": privacy_tier,
               "task_type": decision_meta.get("task_type", ""), "reason": "降级链兜底", "scores": {}}

    # 6. 持久化 + 计量 (真实用量优先, 缺则估算)
    spec = get_model(final_model)
    # falsy-zero 修复: 0 是合法用量, 不能用 or; 显式判断 real_usage 是否含该键
    tokens_in = real_usage["tokens_in"] if "tokens_in" in real_usage else est_in
    tokens_out = real_usage["tokens_out"] if "tokens_out" in real_usage else estimate_tokens(answer)
    # 计量口径: 落 mock 或 is_real=False 时不计费 (cost=0, metered=false); 计费乘档位倍率
    metered = is_real and bool(real_usage)
    mult = quota_decision.state.rate_multiplier if quota_decision else 1.0
    cost = quota.billed_cost(spec, tokens_in, tokens_out, mult) if metered else 0.0
    # KG 三元组抽取【移出事务】(LLM 慢/超时不拉长 BEGIN..COMMIT); sovereign 透传隐私级硬过滤
    # 【移出事件循环】真 provider 下 extract_triples→complete_sync 内含阻塞 ThreadPoolExecutor.result(),
    # 直接在循环线程跑会卡死所有并发 SSE 流 (至多至 20s 超时), 故 to_thread 卸载到线程池
    kg_triples, kg_is_real = await asyncio.to_thread(
        kg.extract_triples, user_text, active_providers=active_providers, privacy_tier=privacy_tier)
    if kg_is_real:  # 出网必留痕: 独立短事务即落即记, 不随归档事务回滚
        dal.log_egress_now(user_id=user_id, action="kg.extract.egress", resource="pending")
    # per-message 元数据快照 (历史加载回填检查器/气泡)
    final_routing = {"chosen_model": final_model, "strategy": strategy,
                     "fallback_used": fallback_used, "fallback_chain": chosen_chain,
                     "privacy_tier": privacy_tier, "task_type": decision_meta.get("task_type", ""),
                     "reason": "降级链兜底" if fallback_used else decision_meta.get("reason", ""),
                     "scores": decision_meta.get("scores", {})}
    usage_meta = {"prompt_tokens": tokens_in, "completion_tokens": tokens_out, "cost_cny": cost,
                  "model": final_model, "fallback_used": fallback_used,
                  "latency_ms": latency_ms, "metered": metered}
    # 6-7 持久化段整体事务包裹: 落库失败整体回滚 (契约E)
    with transaction(conn):
        msg_id = dal.add_message(conn, session_id, "assistant", answer, model=final_model,
                                 routing_meta=final_routing, hits=hits_persist,
                                 injected=injected, usage_meta=usage_meta)
        dal.record_model_call(conn, user_id=user_id, message_id=msg_id, provider=spec.provider,
                              model=final_model, tokens_in=tokens_in, tokens_out=tokens_out, cost_cny=cost,
                              latency_ms=latency_ms, strategy=strategy, fallback_used=fallback_used)
        # 模型推理是唯一受控出网点 (§9): 经 egress 唯一门 (mock/local 内部判 egress=False 不写行)
        egress.audit(conn, user_id=user_id, provider=spec.provider, action="model.infer",
                     resource=final_model, privacy_tier=privacy_tier, is_real=is_real)
        # 7. 归档闭环: 对话入 Vault 作记忆; KG 只抽 user 文本 (防模型输出污染知识图谱)
        archived = service.archive_document(
            conn, user_id=user_id, title=f"对话 · {user_text[:24]}",
            content=f"问：{user_text}\n答：{answer}", doc_type="对话", kg_source=user_text,
            triples=kg_triples, privacy_tier=privacy_tier,
        )
        if kg_is_real:  # 关联 doc_id 的可追溯行 (egress=False, 出网已被上面独立记过, 此处不双计)
            dal.log_audit(conn, user_id=user_id, action="kg.extract.persist",
                          resource=archived["doc_id"], egress=False)
    # [MAJOR-2 二次校验] 落账后真实已用量超硬上限 → 标记 (不回滚本次, 下次 check_quota 即 block)
    if quota_decision and quota.over_hard_cap(conn, user_id):
        events.capture("quota_over_hard_cap", {"model": final_model, "user_id": user_id}, user_id)

    events.capture("route_decided", {"strategy": strategy, "chosen_model": final_model,
                                     "fallback_used": fallback_used, "cost_cny": cost,
                                     "latency_ms": latency_ms}, user_id)
    yield {"type": "usage", "prompt_tokens": tokens_in, "completion_tokens": tokens_out,
           "cost_cny": cost, "model": final_model, "fallback_used": fallback_used,
           "latency_ms": latency_ms, "metered": metered,
           "remaining": quota_decision.state.remaining if quota_decision else None}

    events.capture("vault_archive", {"doc_id": archived["doc_id"], "type": "对话"}, user_id)
    if archived["triples"]:
        events.capture("kg_extracted", {"triples": archived["triples"], "doc_id": archived["doc_id"]}, user_id)
    yield {"type": "vault_archive", **archived}

    # 8. 完成
    yield {"type": "done", "message_id": msg_id, "session_id": session_id}
