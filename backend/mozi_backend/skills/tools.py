"""Skill 工具桥 (契约 C / §8.5 HIGH#1)。

真实工具注册表: skill 声明的 allowed-tools 与本表交集的『只读』工具会被真实执行
(读写真实 Vault/KG), 结果回灌进 system 上下文再调模型。越权/未注册工具被拦截。

设计要点 (信创/本地优先, 零新增依赖):
- 只读工具 (vault_search/kg_query) 用 skill 输入当 query, 确定性, mock 模型下可测。
- 写工具 (vault_archive) 默认不在自动执行集 (AUTO_TOOLS), 仅注册以备显式触发。
- allowed-tools 沙箱: enforce_allowed() 强制 skill.allowed_tools 白名单, 越权即拒。
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from ..config import settings
from ..db import dal
from ..vault import retrieval, service


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., dict[str, Any]]
    readonly: bool
    summary: str


# ---------- 真实工具实现 (读写真实 Vault / KG) ----------

def _vault_search(conn: sqlite3.Connection, user_id: str, query: str, **_: Any) -> dict[str, Any]:
    """只读: 多路检索用户 Vault, 返回命中片段 (走真实 vault.retrieval)。"""
    res = retrieval.search(conn, user_id, query, k=3)
    hits = [{"title": h.title, "text": h.text[:400], "score": round(h.score, 4)} for h in res.hits]
    return {"tool": "vault_search", "query": query, "hits": hits, "count": len(hits)}


def _kg_query(conn: sqlite3.Connection, user_id: str, query: str, **_: Any) -> dict[str, Any]:
    """只读: 以输入为实体名查 KG 子图 (走真实 dal.query_kg)。"""
    sub = dal.query_kg(conn, user_id, query, hops=1)
    return {"tool": "kg_query", "entity": query, "nodes": sub["nodes"], "edges": sub["edges"]}


def _vault_archive(conn: sqlite3.Connection, user_id: str, query: str, **_: Any) -> dict[str, Any]:
    """写: 把输入归档为 Vault 文档 (走真实 vault.service.archive_document)。"""
    stat = service.archive_document(
        conn, user_id=user_id, title="skill · 归档", content=query, doc_type="skill"
    )
    return {"tool": "vault_archive", "doc_id": stat["doc_id"], "chunks": stat["chunks"]}


# ---------- 联网搜索工具 (keyless, 不依赖 Ark 付费插件) ----------

_DDG_URL = "https://html.duckduckgo.com/html/"
# 单条结果块: 标题(必) + 摘要(可选, 锚在同块, 不跨到下一标题) —— 防标题/摘要按下标错位 (F1)
_RX_RESULT = re.compile(
    r'class="result__a"[^>]*>(.*?)</a>'
    r'(?:(?:(?!class="result__a").)*?class="result__snippet"[^>]*>(.*?)</a>)?',
    re.S)


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _parse_ddg(text: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for title, snip in _RX_RESULT.findall(text):
        t = _strip_html(title)
        if t:
            out.append({"title": t, "snippet": _strip_html(snip)})
    return out


def _http_get(url: str, params: dict, headers: dict) -> str:
    """同步 HTTP GET (独立函数, 便于测试注入 MockTransport / monkeypatch)。"""
    with httpx.Client(timeout=15.0, follow_redirects=True) as c:
        return c.get(url, params=params, headers=headers).text


# ---------- 可插拔搜索后端 (SearXNG 自托管 → ddgs 库 → DDG 爬虫兜底) ----------

_NEWS_HINT = ("新闻", "热点", "头条", "最新", "今日", "news", "today", "latest")


def _search_searxng(query: str) -> list[dict[str, str]]:
    """自托管 SearXNG JSON API (信创主权: 可配国产引擎, 完全自主)。"""
    base = (settings.searxng_url or "").rstrip("/")
    text = _http_get(f"{base}/search",
                     {"q": query, "format": "json", "language": "zh-CN"},
                     {"User-Agent": "mozi/web_search"})
    data = json.loads(text)
    return [{"title": r.get("title", ""), "snippet": (r.get("content") or "")[:400],
             "url": r.get("url", "")} for r in data.get("results", [])]


def _search_ddgs(query: str) -> list[dict[str, str]]:
    """ddgs 元搜索库 (keyless, 多引擎)。新闻类查询优先 news(), 失败回退 text()。"""
    from ddgs import DDGS  # 惰性导入: 未装则上层回退爬虫

    rows: list[dict[str, str]] = []
    if any(k in query.lower() for k in _NEWS_HINT):
        try:
            rows = [{"title": x.get("title", ""), "snippet": (x.get("body") or "")[:400],
                     "url": x.get("url", ""), "date": str(x.get("date", ""))[:10]}
                    for x in DDGS().news(query, max_results=5)]
        except Exception:  # noqa: BLE001 — news 无结果时回退 text
            rows = []
    if not rows:
        rows = [{"title": x.get("title", ""), "snippet": (x.get("body") or "")[:400],
                 "url": x.get("href", "")} for x in DDGS().text(query, max_results=5)]
    return rows


def _search_ddg_html(query: str) -> list[dict[str, str]]:
    """兜底: 无依赖的 DuckDuckGo HTML 解析 (keyless)。"""
    return _parse_ddg(_http_get(_DDG_URL, {"q": query}, {"User-Agent": "Mozilla/5.0"}))


def _resolve_backend() -> str:
    if settings.searxng_url:
        return "searxng"
    try:
        import ddgs  # noqa: F401
        return "ddgs"
    except ImportError:
        return "ddg_html"


def _web_search(conn: sqlite3.Connection, user_id: str, query: str, *,
                privacy_tier: str = "local_first", **_: Any) -> dict[str, Any]:
    """只读但【外呼】: 可插拔联网搜索。出网受控点 → 写 egress 审计 (§9)。

    后端优先级: SearXNG(配 SEARXNG_URL) → ddgs 库 → DDG 爬虫。主后端失败自动回退爬虫。
    privacy_tier 透传至 egress.audit (agentic 循环下按真实隐私级审计, 不再硬编码 local_first)。
    """
    from ..gateway import egress
    # 外呼前先过 egress 硬门: sovereign 且无自托管引擎 → allowed=False, 绝不发起网络请求。
    v = egress.classify(provider="web_search", privacy_tier=privacy_tier, is_real=True)
    if not v.allowed:
        return {"tool": "web_search", "query": query, "backend": "blocked",
                "results": [], "count": 0, "error": "egress_blocked"}
    backend = _resolve_backend()
    dispatch = {"searxng": _search_searxng, "ddgs": _search_ddgs, "ddg_html": _search_ddg_html}
    results: list[dict[str, str]] = []
    try:
        results = dispatch[backend](query)[:5]
    except Exception:  # noqa: BLE001 — 主后端失败 → 回退无依赖爬虫, 不阻断 skill
        if backend == "ddg_html":  # 主后端已是爬虫, 别重试同一函数 (F4)
            backend = "ddg_html->fail"
        else:
            try:
                results = _search_ddg_html(query)[:5]
                backend = f"{backend}->ddg_html"
            except Exception:  # noqa: BLE001
                results = []
                backend = f"{backend}->fail"
    # web_search 是模型推理之外的第二个受控出网点, 经 egress 唯一门审计
    egress.audit(conn, user_id=user_id, provider="web_search", action="tool.web_search",
                 resource=query[:80], privacy_tier=privacy_tier, is_real=True)
    return {"tool": "web_search", "query": query, "backend": backend,
            "results": results, "count": len(results)}


# 注册表 (契约 C): vault_search / kg_query / vault_archive / web_search
TOOLS: dict[str, ToolSpec] = {
    "vault_search": ToolSpec("vault_search", _vault_search, True, "检索用户 Vault 片段"),
    "kg_query": ToolSpec("kg_query", _kg_query, True, "查询知识图谱子图"),
    "vault_archive": ToolSpec("vault_archive", _vault_archive, False, "归档文本到 Vault"),
    "web_search": ToolSpec("web_search", _web_search, True, "keyless 联网搜索 (外呼, 审计)"),
}

# 自动执行集: 只读工具才会在 invoke 时用 user input 当 query 自动触发 (写工具需显式)。
AUTO_TOOLS: set[str] = {name for name, spec in TOOLS.items() if spec.readonly}


def register(spec: ToolSpec, *, override: bool = False) -> None:
    """注册工具到 TOOLS/AUTO_TOOLS。重名非 override → ValueError。"""
    if spec.name in TOOLS and not override:
        raise ValueError(f"tool {spec.name} already registered")
    TOOLS[spec.name] = spec
    if spec.readonly:
        AUTO_TOOLS.add(spec.name)
    else:
        AUTO_TOOLS.discard(spec.name)


def unregister(name: str) -> None:
    TOOLS.pop(name, None)
    AUTO_TOOLS.discard(name)


def is_registered(tool: str) -> bool:
    return tool in TOOLS


def enforce_allowed(tool: str, allowed_tools: list[str]) -> bool:
    """allowed-tools 沙箱强制: 工具须同时在注册表 且 在 skill 白名单内。"""
    return is_registered(tool) and tool in allowed_tools


def plan_tools(allowed_tools: list[str]) -> list[str]:
    """返回本次 invoke 会真实执行的只读工具序列 (allowed ∩ 注册表 ∩ 自动集), 保持声明顺序。"""
    seen: set[str] = set()
    plan: list[str] = []
    for t in allowed_tools:
        if t in seen:
            continue
        seen.add(t)
        if enforce_allowed(t, allowed_tools) and t in AUTO_TOOLS:
            plan.append(t)
    return plan


def execute(tool: str, conn: sqlite3.Connection, user_id: str, query: str) -> dict[str, Any]:
    """执行已通过沙箱校验的工具 (调用方须先 enforce_allowed)。"""
    return TOOLS[tool].fn(conn, user_id, query)


def tools_schema(allowed_tools: list[str]) -> list[dict[str, Any]]:
    """allowed ∩ 注册表 → OpenAI function-calling schema 列表 (喂模型, 供 agentic 循环按需调)。

    本期工具均单参 query; 后续多参工具扩各自 parameters。未注册工具跳过 (沙箱前置)。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in allowed_tools:
        if t in seen or not is_registered(t):
            continue
        seen.add(t)
        out.append({"type": "function", "function": {
            "name": t, "description": TOOLS[t].summary,
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]}}})
    return out


def execute_call(tool: str, conn: sqlite3.Connection, user_id: str, args: dict[str, Any],
                 *, privacy_tier: str = "local_first") -> dict[str, Any]:
    """tool-call 的结构化 args(dict) → 现有 fn(query) 桥接 (调用方须先 enforce_allowed)。

    privacy_tier 透传给工具 (出网类工具如 web_search 按真实隐私级审计; 本地工具经 **_ 忽略)。
    """
    return TOOLS[tool].fn(conn, user_id, args.get("query", ""), privacy_tier=privacy_tier)


def format_context(results: list[dict[str, Any]]) -> str:
    """把工具结果序列化为回灌进 system 的上下文文本 (供模型参考)。"""
    if not results:
        return ""
    lines = ["[工具检索结果 · 供参考]"]
    for r in results:
        tool = r.get("tool", "tool")
        if tool == "vault_search":
            lines.append(f"- vault_search({r.get('query','')}): 命中 {r.get('count', 0)} 条")
            for h in r.get("hits", []):
                lines.append(f"  · {h.get('title','')}: {h.get('text','')}")
        elif tool == "kg_query":
            edges = r.get("edges", [])
            lines.append(f"- kg_query({r.get('entity','')}): {len(edges)} 条关系")
            for e in edges:
                lines.append(f"  · {e.get('subject','')} -{e.get('predicate','')}-> {e.get('object','')}")
        elif tool == "vault_archive":
            lines.append(f"- vault_archive: 已归档 doc={r.get('doc_id','')}")
        elif tool == "web_search":
            lines.append(f"- web_search({r.get('query','')}): {r.get('count', 0)} 条联网结果")
            for it in r.get("results", []):
                # 带出来源 url/date 供模型标注出处 (SKILL.md 要求); 兜底爬虫无 url 则省略 (F2)
                meta = " · ".join(x for x in (it.get("date"), it.get("url")) if x)
                suffix = f" [{meta}]" if meta else ""
                lines.append(f"  · {it.get('title','')}{suffix}: {it.get('snippet','')}")
    return "\n".join(lines)
