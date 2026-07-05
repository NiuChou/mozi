"""端到端冒烟测试 (无需起服务, 用 TestClient)。验证 §8.4 对话—路由—知识闭环。

跑: backend/.venv/bin/python smoke_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

# 用临时库, 不污染开发库
_TMP = tempfile.mkdtemp(prefix="mozi_smoke_")
os.environ["MOZI_DATA_DIR"] = _TMP
os.environ["MOZI_DB_PATH"] = os.path.join(_TMP, "smoke.db")
os.environ.setdefault("MOZI_LOCAL_FIRST", "1")
# 冒烟须 hermetic 零外呼: 阻断 config 自动加载真实 .env.local (否则真 key 激活 provider → 打真实模型)
os.environ.setdefault("MOZI_ENV_FILE", os.path.join(_TMP, ".env.absent"))

from fastapi.testclient import TestClient  # noqa: E402

from mozi_backend.main import app  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return events


def main() -> int:
    with TestClient(app) as client:
        print("\n[1] 健康检查 + 迁移/种子")
        r = client.get("/health")
        check("health ok", r.status_code == 200 and r.json()["status"] == "ok")

        r = client.get("/v1/models")
        check("模型目录 ≥6", len(r.json()["models"]) >= 6)

        print("\n[2] Vault 归档 (分块 + 向量 + KG 抽取)")
        doc = client.post("/v1/vault/archive", json={
            "title": "墨子架构", "type": "笔记",
            "content": "墨子是本地优先的桌面应用。UMA 是多模型路由网关。Vault 使用 SQLite。"
                       "墨子依赖 BGE-M3。检索引擎使用 RRF 融合。",
        }).json()
        check("归档产出 chunks", doc.get("chunks", 0) >= 1, str(doc))
        check("KG 抽到三元组", doc.get("triples", 0) >= 1, str(doc))

        print("\n[3] Vault 多路检索 (BM25 + dense + RRF)")
        s = client.post("/v1/vault/search", json={"query": "UMA 路由网关", "k": 3}).json()
        check("检索有命中", len(s.get("hits", [])) >= 1, str(s))
        check("命中带出处", bool(s["hits"]) and "provenance" in s["hits"][0])

        print("\n[4] Mozi-KG 子图查询")
        kg = client.post("/v1/kg/query", json={"entity": "墨子"}).json()
        check("KG 子图有节点", len(kg.get("nodes", [])) >= 1, str(kg))

        print("\n[5] 路由预览 (UMA 决策, 不调模型)")
        rp = client.post("/v1/route-preview", json={
            "text": "def foo(): pass  # 帮我重构这段代码", "policy": "auto"}).json()
        check("代码任务路由命中", rp["task_type"] == "code", str(rp))
        sov = client.post("/v1/route-preview", json={"text": "你好", "privacy_tier": "sovereign"}).json()
        # 国产集动态取自模型目录 (domestic 标志), 不硬编码 → 新增国产模型不致测试过时
        _DOMESTIC = {m["id"] for m in client.get("/v1/models").json()["models"] if m["domestic"]}
        check("信创级只选国产", sov["chosen_model"] in _DOMESTIC, str(sov))
        # 强化: 整条 fallback_chain 全国产, claude/gpt 不得出现
        _chain = sov.get("fallback_chain", [])
        check("信创级整条链全国产", bool(_chain) and all(m in _DOMESTIC for m in _chain), str(_chain))
        check("信创级链无 claude/gpt", "claude" not in _chain and "gpt" not in _chain, str(_chain))

        print("\n[6] Chat 流式闭环 (路由→注入→流式→归档→计量)")
        r = client.post("/v1/chat", json={
            "messages": [{"role": "user", "content": "墨子用什么做数据库？"}],
            "inject_context": True, "routing": {"policy": "auto"}})
        evts = parse_sse(r.text)
        types = [e["type"] for e in evts]
        check("有路由元数据", "routing_metadata" in types, str(types))
        check("有流式增量", "delta" in types)
        check("有检索事件", "retrieval" in types)
        check("有计量事件", "usage" in types)
        check("有归档事件", "vault_archive" in types)
        check("有完成事件", "done" in types)
        usage_evt = next((e for e in evts if e["type"] == "usage"), {})
        check("计量含成本", "cost_cny" in usage_evt, str(usage_evt))
        check("计量含延迟", "latency_ms" in usage_evt, str(usage_evt))
        check("计量标真实/估算", "metered" in usage_evt, str(usage_evt))
        # 强化: 无 key → 走 mock → metered=False 且 cost=0 (本地优先零计费)
        check("mock 链路 metered=False", usage_evt.get("metered") is False, str(usage_evt))
        check("mock 链路 cost=0", usage_evt.get("cost_cny") == 0.0, str(usage_evt))

        print("\n[7] 用量入账")
        u = client.get("/v1/usage").json()
        check("用量 tokens 计入", u["tokens_used"] > 0, str(u))
        check("请求数计入", u["requests"] >= 1)

        print("\n[8] Skill 兼容层 (discover → list → load → invoke)")
        disc = client.post("/v1/skills/discover").json()
        check("发现 ≥1 skill", disc["discovered"] >= 1, str(disc.get("discovered")))
        skills = client.get("/v1/skills").json()["skills"]
        check("列表带 capability", bool(skills) and "capability" in skills[0])
        demo = next((s for s in skills if s["name"] == "mozi-demo"), skills[0])
        loaded = client.post("/v1/skills/load", json={"skill_id": demo["skill_id"]}).json()
        check("载入正文", len(loaded.get("instructions", "")) > 0)
        inv = client.post("/v1/skills/invoke", json={
            "skill_id": demo["skill_id"], "input": "墨子是本地优先应用，使用 SQLite。"}).json()
        check("skill 调用选模 + 产物", inv.get("status") == "ok" and len(inv.get("output", "")) > 0, str(inv)[:200])

        print("\n[9] 审计 / 遥测")
        ev = client.get("/v1/events").json()["events"]
        check("遥测事件入库", len(ev) >= 1)
        rd = next((e for e in ev if e["event"] == "route_decided"), {})
        check("route_decided 含 fallback_used", "fallback_used" in rd.get("props", {}), str(rd))
        cs = next((e for e in ev if e["event"] == "chat_send"), {})
        check("chat_send 含 injected", "injected" in cs.get("props", {}), str(cs))

        print("\n[10] 降级链容错 (主模型强制失败 → 兜底)")
        import mozi_backend.gateway.orchestrator as orch  # noqa: E402
        from mozi_backend.gateway.adapters.mock import MockAdapter  # noqa: E402

        _mock = MockAdapter()
        _orig = orch.select_adapter

        class _Boom:
            async def astream(self, s, m, usage=None):
                raise RuntimeError("forced primary failure")
                yield ""  # pragma: no cover

        def _patched(spec):
            if spec.id == "glm-5.2":      # 链首强制失败
                return _Boom(), True
            return _mock, False

        orch.select_adapter = _patched
        try:
            r = client.post("/v1/chat", json={
                "messages": [{"role": "user", "content": "测试降级链"}],
                "model": "glm-5.2", "inject_context": False})
            evts = parse_sse(r.text)
            types = [e["type"] for e in evts]
            ue = next((e for e in evts if e["type"] == "usage"), {})
            check("触发 fallback 事件", "fallback" in types, str(types))
            check("降级后仍完成", "done" in types)
            check("usage.fallback_used=True", ue.get("fallback_used") is True, str(ue))
            check("兜底模型非 glm-5.2", ue.get("model") not in (None, "glm-5.2"), str(ue))
        finally:
            orch.select_adapter = _orig

        print("\n[11] 上下文裁剪 (max_context 极小 → 不报错且出答案)")
        r = client.post("/v1/chat", json={
            "messages": [{"role": "user", "content": "裁剪验证一下上下文预算"}],
            "inject_context": False, "routing": {"policy": "balanced", "max_context": 64}})
        evts = parse_sse(r.text)
        check("极小窗口仍出答案", "done" in [e["type"] for e in evts])
        ue = next((e for e in evts if e["type"] == "usage"), {})
        check("裁剪后 prompt_tokens 受限", ue.get("prompt_tokens", 9999) <= 200, str(ue))

        print("\n[12] model_switch 埋点 (手动切模型)")
        sess = client.post("/v1/sessions", json={"title": "切模型"}).json()["session_id"]
        client.post("/v1/chat", json={"messages": [{"role": "user", "content": "第一轮"}],
                                      "session_id": sess, "model": "glm-5.2", "inject_context": False})
        client.post("/v1/chat", json={"messages": [{"role": "user", "content": "第二轮换模型"}],
                                      "session_id": sess, "model": "deepseek-v4", "inject_context": False})
        ev2 = client.get("/v1/events").json()["events"]
        ms = next((e for e in ev2 if e["event"] == "model_switch"), {})
        check("model_switch 已埋点", ms.get("props", {}).get("to_model") == "deepseek-v4", str(ms))

        print("\n[13] Egress 审计 (mock 链路零出网)")
        au = client.get("/v1/audit?limit=100").json()["audit"]
        check("审计有归档记录", any(a["action"] == "vault.archive" for a in au), str(au)[:120])
        arch = [a for a in au if a["action"] == "vault.archive"]
        check("归档 egress_flag=0", bool(arch) and all(a["egress_flag"] == 0 for a in arch), str(arch)[:120])
        check("mock 链路无 egress=1", all(a["egress_flag"] == 0 for a in au), str(au)[:120])
        check("mock 不记 model.infer", not any(a["action"] == "model.infer" for a in au), str(au)[:120])

        print("\n[14] KG 特定三元组 (强断言, 非仅计数)")
        # 归档已知文本 (换行分隔使主语锚定行首), 断言抽到确切三元组
        client.post("/v1/vault/archive", json={
            "title": "三元组样本", "type": "笔记",
            "content": "墨子依赖 BGE-M3。\nUMA 是多模型路由网关。"})
        # 以 "墨子" 为中心 (精确实体名, 避免 LIKE 命中含 BGE-M3 的复合实体)
        kg2 = client.post("/v1/kg/query", json={"entity": "墨子"}).json()
        trips = {(e["subject"], e["predicate"], e["object"]) for e in kg2.get("edges", [])}
        check("KG 含 (墨子,依赖,BGE-M3)", ("墨子", "依赖", "BGE-M3") in trips, str(trips)[:200])

        print("\n[15] 数据主权 export / delete (契约 D)")
        exp = client.get("/v1/export").json()
        check("export schema 正确", exp.get("schema") == "mozi.export.v1", str(exp.get("schema")))
        check("export 含 sessions", len(exp["data"]["sessions"]) >= 1, str(len(exp["data"]["sessions"])))
        check("export 含 vault 文档", len(exp["data"]["vault_documents"]) >= 1)
        check("export 含 KG 实体", len(exp["data"]["kg_entities"]) >= 1)
        check("export 含用量", len(exp["data"]["usage"]) >= 1)

    print(f"\n{'='*40}\n  PASS={PASS}  FAIL={FAIL}\n{'='*40}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
