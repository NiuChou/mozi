"""Skill 兼容层 API (§7.3 / §8.5): discover / list / load / invoke。统一 /v1/skills/*。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends

from ..auth import current_user_id
from ..config import settings
from ..db import dal
from ..db.database import get_conn, transaction
from ..gateway import agent_loop, egress, quota
from ..gateway.adapters.base import estimate_tokens
from ..gateway.adapters.registry import select_adapter
from ..gateway.models import get_model
from ..gateway.router import RouteRequest, route
from ..schemas import SkillInvokeRequest, SkillLoadRequest
from ..telemetry import events
from ..util import jload, new_id
from ..vault import service
from . import loader, tools


def _loop_enabled() -> bool:
    """MOZI_AGENT_LOOP 灰度开关 (默认 0 = 旧静态预取路径)。"""
    return os.getenv("MOZI_AGENT_LOOP", "0") == "1"

router = APIRouter(prefix="/v1/skills", tags=["skills"])


@router.get("/tools")
def list_tools(_user_id: str = Depends(current_user_id)):
    """工具注册表元数据 (契约 C)。"""
    return {"tools": [{"name": s.name, "readonly": s.readonly, "summary": s.summary,
                       "auto": s.name in tools.AUTO_TOOLS} for s in tools.TOOLS.values()]}


@router.get("")
def list_skills(user_id: str = Depends(current_user_id)):
    """①启动级: 仅 name + description + 元数据 (≈100 tokens/skill)。"""
    with get_conn() as conn:
        rows = dal.list_skills(conn)
    return {"skills": [{
        "skill_id": r["skill_id"], "name": r["name"], "source": r["source"],
        "tier": r["tier"], "version": r["version"], "scan_status": r["scan_status"],
        "auto_invoke": bool(r["auto_invoke"]),
        "capability": jload(r["capability"], {}),
        "allowed_tools": jload(r["allowed_tools"], []),
    } for r in rows]}


@router.post("/discover")
def discover(user_id: str = Depends(current_user_id)):
    """并行扫描 .claude/.codex/.mozi 来源, 命中 SKILL.md 即登记。"""
    descriptors = loader.discover()
    out = []
    with get_conn() as conn:
        for d in descriptors:
            skill_id = dal.upsert_skill(
                conn, name=d.name, source=d.source, origin_path=d.origin_path, version=d.version,
                tier=d.tier, capability=d.capability, allowed_tools=d.allowed_tools,
                auto_invoke=d.auto_invoke, scan_status=d.scan_status,
            )
            events.capture("skill_loaded", {"skill_id": skill_id, "source": d.source,
                                            "tier": d.tier, "scan_ok": d.scan_status == "ok"}, user_id)
            out.append({"skill_id": skill_id, "name": d.name, "source": d.source,
                        "tier": d.tier, "scan_status": d.scan_status})
    return {"discovered": len(out), "skills": out}


@router.post("/load")
def load_skill(body: SkillLoadRequest, user_id: str = Depends(current_user_id)):
    """②激活级: 载入 SKILL.md 正文; ③资源级 (level>=3): 附带 scripts/references 资源清单。"""
    with get_conn() as conn:
        skill = dal.get_skill(conn, body.skill_id)
    if not skill:
        return {"error": "skill not found"}
    text = ""
    p = Path(skill["origin_path"])
    if p.exists():
        _, text = loader.parse_frontmatter(p.read_text(encoding="utf-8"))
    resp = {"skill_id": body.skill_id, "name": skill["name"], "tier": skill["tier"],
            "instructions": text[:8000], "allowed_tools": jload(skill["allowed_tools"], [])}
    if body.level >= 3 and p.exists():
        resp["resources"] = loader.list_resources(p.parent)
    return resp


async def _exec_static(conn, *, body, skill_id, instructions, allowed_tools, spec, is_real,
                       privacy, user_id) -> dict:
    """旧静态路径: 预取只读工具结果回灌 system → 单次模型流 (MOZI_AGENT_LOOP=0)。"""
    for t in allowed_tools:                       # 越权/未注册预登记 tool_denied
        if not tools.enforce_allowed(t, allowed_tools) and t not in tools.AUTO_TOOLS \
                and not tools.is_registered(t):
            events.capture("skill_error", {"skill_id": skill_id, "type": "tool_denied", "tool": t}, user_id)
    tool_results: list[dict] = []
    tools_used: list[str] = []
    for t in tools.plan_tools(allowed_tools):
        if not tools.enforce_allowed(t, allowed_tools):
            events.capture("skill_error", {"skill_id": skill_id, "type": "tool_denied", "tool": t}, user_id)
            continue
        try:
            # execute_call 透传 privacy_tier → 出网类工具 (web_search) 按真实隐私级审计, 不再硬编码 local_first
            tool_results.append(tools.execute_call(t, conn, user_id, {"query": body.input},
                                                   privacy_tier=privacy))
            tools_used.append(t)
        except Exception:  # noqa: BLE001
            events.capture("skill_error", {"skill_id": skill_id, "type": "tool_error", "tool": t}, user_id)

    tool_ctx = tools.format_context(tool_results)
    system_text = instructions[:6000] + (f"\n\n{tool_ctx}" if tool_ctx else "")
    convo = [{"role": "system", "content": system_text}, {"role": "user", "content": body.input}]
    adapter, _ = select_adapter(spec)
    output, status = "", "ok"
    sk_usage: dict = {}
    t0 = time.perf_counter()
    try:
        async for delta in adapter.astream(spec, convo, sk_usage):
            output += delta
    except Exception as exc:  # noqa: BLE001
        status = "error"
        events.capture("skill_error", {"skill_id": skill_id, "type": "model", "tool": None}, user_id)
        output = f"[skill 调用降级] {exc}"
    tok_in = sk_usage["tokens_in"] if "tokens_in" in sk_usage else (estimate_tokens(system_text) + estimate_tokens(body.input))
    tok_out = sk_usage["tokens_out"] if "tokens_out" in sk_usage else estimate_tokens(output)
    return {"output": output, "tools_used": tools_used, "status": status, "run_id": None, "steps": 0,
            "tokens_in": tok_in, "tokens_out": tok_out, "metered": is_real and bool(sk_usage),
            "latency_ms": int((time.perf_counter() - t0) * 1000)}


async def _exec_loop(conn, *, body, instructions, allowed_tools, spec, is_real, privacy, user_id) -> dict:
    """有界 agentic 工具循环 (MOZI_AGENT_LOOP=1): 模型按需调工具迭代。model.infer 由循环逐步审计。"""
    run_id = new_id("run")
    convo = [{"role": "system", "content": instructions[:6000]},
             {"role": "user", "content": body.input}]
    output, status = "", "ok"
    tools_used: list[str] = []
    t0 = time.perf_counter()
    async for ev in agent_loop.run_tool_loop(conn, user_id=user_id, convo=convo,
                                             allowed_tools=allowed_tools, spec=spec, run_id=run_id,
                                             privacy_tier=privacy):
        et = ev["type"]
        if et == "delta":
            output += ev["text"]
        elif et == "tool_result" and "error" not in ev["result"] and ev["name"] not in tools_used:
            tools_used.append(ev["name"])
        elif et == "egress_denied":
            status, output = "blocked", f"[出网被拒] {ev.get('reason', '')}"
    steps = dal.list_agent_steps(conn, run_id)
    tok_in = sum(s["tokens_in"] for s in steps) or (estimate_tokens(instructions[:6000]) + estimate_tokens(body.input))
    tok_out = sum(s["tokens_out"] for s in steps) or estimate_tokens(output)
    return {"output": output, "tools_used": tools_used, "status": status, "run_id": run_id,
            "steps": len(steps), "tokens_in": tok_in, "tokens_out": tok_out,
            "metered": is_real and any(s["tokens_in"] or s["tokens_out"] for s in steps),
            "latency_ms": int((time.perf_counter() - t0) * 1000)}


@router.post("/invoke")
async def invoke_skill(body: SkillInvokeRequest, user_id: str = Depends(current_user_id)):
    """③ 运行时 (§8.5.9 真实工具闭环): scan/配额门 → 选模 → 执行 (有界循环 or 旧静态) → 入库归档。

    MOZI_AGENT_LOOP=1 且 skill 声明 allowed-tools 且模型支持 → 走 agentic 循环 (模型自主调工具迭代);
    否则走旧静态路径 (预取只读工具一次回灌)。两路共用 scan/配额/选模/持久化。
    """
    with get_conn() as conn:
        # 全新 multiuser 用户首请求即 invoke 时, skill_calls/vault_documents 的
        # user_id REFERENCES users → 须先 ensure_user (对齐 gateway/vault), 否则 FK 失败 500。
        dal.ensure_user(conn, user_id, f"{user_id}@mozi.local", settings.default_region)
        skill = dal.get_skill(conn, body.skill_id)
        if not skill:
            return {"error": "skill not found"}
        cap = jload(skill["capability"], {})
        allowed_tools = jload(skill["allowed_tools"], [])

        # (1) scan 门: warn 或 tier C 需显式 confirm
        passed, reason = loader.scan_gate(skill["scan_status"], body.confirm, tier=skill["tier"])
        if not passed:
            events.capture("skill_error", {"skill_id": body.skill_id, "type": "scan_blocked",
                                           "tool": None}, user_id)
            return {"skill_id": body.skill_id, "name": skill["name"], "tier": skill["tier"],
                    "status": "blocked", "reason": reason, "scan_status": skill["scan_status"],
                    "tools_used": [], "output": "", "cost_cny": 0.0, "run_id": None, "steps": 0,
                    "hint": "tier=C(越权/警示) 或 scan_status=warn, 需 confirm=true 才可执行"}

        # (1b) 配额门: 硬上限 block 直接拒绝
        privacy = "sovereign" if settings.local_first else "local_first"
        qd = quota.check_quota(conn, user_id, est_tokens=estimate_tokens(body.input) + 500,
                               privacy_tier=privacy)
        if qd.action == "block":
            events.capture("skill_error", {"skill_id": body.skill_id, "type": "quota_blocked",
                                           "tool": None}, user_id)
            return {"skill_id": body.skill_id, "name": skill["name"], "tier": skill["tier"],
                    "status": "blocked", "code": "quota_exceeded", "reason": qd.reason,
                    "tools_used": [], "output": "", "cost_cny": 0.0, "run_id": None, "steps": 0}

        # (2) 选模 (Capability→策略, 两路共用); degrade 强制降级模型
        policy = "code" if cap.get("code") else ("quality" if cap.get("reasoning_high") else "balanced")
        p = Path(skill["origin_path"])
        instructions = ""
        if p.exists():
            _, instructions = loader.parse_frontmatter(p.read_text(encoding="utf-8"))
        decision = route(RouteRequest(policy=policy, privacy_tier=privacy,
                                      est_tokens=estimate_tokens(body.input) + 500, text=body.input,
                                      active_providers=set(settings.active_providers())))
        chosen_model = qd.forced_model if (qd.action == "degrade" and qd.forced_model) else decision.chosen_model
        spec = get_model(chosen_model)
        _, is_real = select_adapter(spec)

        # (3) 执行: 有界循环 (门控+声明工具+模型支持) or 旧静态
        looped = _loop_enabled() and bool(allowed_tools) and spec.supports_tools
        if looped:
            r = await _exec_loop(conn, body=body, instructions=instructions, allowed_tools=allowed_tools,
                                 spec=spec, is_real=is_real, privacy=privacy, user_id=user_id)
        else:
            r = await _exec_static(conn, body=body, skill_id=body.skill_id, instructions=instructions,
                                   allowed_tools=allowed_tools, spec=spec, is_real=is_real,
                                   privacy=privacy, user_id=user_id)
        cost = quota.billed_cost(spec, r["tokens_in"], r["tokens_out"], qd.state.rate_multiplier) if r["metered"] else 0.0

        # (4) 落库 + 归档 (transaction 包裹)。循环路径 model.infer 已逐步审计 → 不再 skill.infer 双计。
        session_ok = bool(body.session_id) and dal.get_session(conn, user_id, body.session_id) is not None
        with transaction(conn):
            if looped:
                # 循环路径 model.infer 已逐步审计; blocked (sovereign 出网被拒) 时零字节出网 → flag=0
                egress_flag = is_real and r["status"] != "blocked"
            else:
                egress_flag = egress.audit(conn, user_id=user_id, provider=spec.provider,
                                           action="skill.infer", resource=body.skill_id,
                                           privacy_tier=privacy, is_real=is_real).egress
            msg_id = (dal.add_message(conn, body.session_id, "assistant", r["output"], model=chosen_model)
                      if session_ok else None)
            archived = service.archive_document(conn, user_id=user_id, title=f"skill · {skill['name']}",
                                                content=f"输入：{body.input}\n产物：{r['output']}", doc_type="skill")
            dal.record_skill_call(conn, user_id=user_id, skill_id=body.skill_id, session_id=body.session_id,
                                  chosen_model=chosen_model, tools_used=r["tools_used"],
                                  tokens_in=r["tokens_in"], tokens_out=r["tokens_out"], cost_cny=cost,
                                  latency_ms=r["latency_ms"], status=r["status"], egress=egress_flag,
                                  message_id=msg_id, archived_doc_id=archived["doc_id"])
        events.capture("skill_invoked", {"skill_id": body.skill_id, "auto": body.auto,
                                         "chosen_model": chosen_model, "looped": looped,
                                         "tools_used": r["tools_used"], "cost_cny": cost}, user_id)

    return {"skill_id": body.skill_id, "name": skill["name"], "tier": skill["tier"],
            "chosen_model": chosen_model, "strategy": decision.strategy,
            "capability": cap, "tools_used": r["tools_used"], "status": r["status"],
            "output": r["output"], "cost_cny": cost, "metered": r["metered"],
            "run_id": r["run_id"], "steps": r["steps"]}
