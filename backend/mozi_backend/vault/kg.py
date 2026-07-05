"""Mozi-KG 三元组抽取: 真实 LLM 优先 + 正则回退 (零 key 降级)。

extract_triples(text, *, active_providers, privacy_tier) -> (triples, is_real)。
is_real=True 表实际命中真实 LLM (供上层补 egress 审计 —— 出网即记, 与抽到与否无关)。
向后兼容: 不传 active_providers (旧调用) → 纯正则, 返回 (4元组列表, False)。
"""
from __future__ import annotations

import json
import re
import unicodedata

# 中文谓词模式: "A 是 B" / "A 有 B" / "A 属于 B" / "A 包含 B" / "A 使用 B"
_ZH_PATTERNS = [
    (re.compile(r"(.{2,12}?)\s*是\s*(.{2,20}?)[。,，;；\n]"), "是"),
    (re.compile(r"(.{2,12}?)\s*有\s*(.{2,20}?)[。,，;；\n]"), "有"),
    (re.compile(r"(.{2,12}?)\s*属于\s*(.{2,20}?)[。,，;；\n]"), "属于"),
    (re.compile(r"(.{2,12}?)\s*包含\s*(.{2,20}?)[。,，;；\n]"), "包含"),
    (re.compile(r"(.{2,12}?)\s*使用\s*(.{2,20}?)[。,，;；\n]"), "使用"),
    (re.compile(r"(.{2,12}?)\s*依赖\s*(.{2,20}?)[。,，;；\n]"), "依赖"),
]
# 英文: "A is B" / "A uses B" / "A has B"
_EN_PATTERNS = [
    (re.compile(r"\b([A-Z][\w\- ]{1,30}?)\s+is\s+(?:a |an |the )?([\w\- ]{2,30}?)[.,;\n]", re.I), "is"),
    (re.compile(r"\b([A-Z][\w\- ]{1,30}?)\s+uses?\s+([\w\- ]{2,30}?)[.,;\n]", re.I), "uses"),
    (re.compile(r"\b([A-Z][\w\- ]{1,30}?)\s+has\s+([\w\- ]{2,30}?)[.,;\n]", re.I), "has"),
]

_STOP = {"the", "a", "an", "this", "that", "it", "这", "那", "它", "他", "她"}


def _clean(s: str) -> str:
    return s.strip(" \t　·•-—:：").strip()


# 谓词归一 canonical 表 (长尾扩表数据驱动: 见 dal.predicate_histogram)
_PRED_CANON = {"is": "是", "isa": "是", "为": "是", "uses": "使用", "use": "使用", "利用": "使用",
               "depends on": "依赖", "rely on": "依赖", "has": "有", "contains": "包含",
               "include": "包含", "belongs to": "属于", "part of": "属于"}

EXTRACT_SYSTEM = (
    "你是知识图谱抽取器。从文本抽取事实三元组, 只输出 JSON 数组, 每元素 "
    "{subject,predicate,object,confidence,subject_type,object_type}; confidence∈[0,1]; 无事实输出 []。"
    " predicate 优先取以下之一: 是/有/属于/包含/使用/依赖/位于/创建/隶属; 不在表内时用最简短动词短语, 不要整句。")


def normalize_predicate(p: str) -> str:
    """谓词归一: 全角转半角 + 去首尾空白与尾标点 (降纯排版碎片) → canonical 表。"""
    if not p:
        return p
    p = unicodedata.normalize("NFKC", p).strip().strip(" 。.,，、;；:：")
    key = p.lower() if p.isascii() else p
    return _PRED_CANON.get(key, p)


def _extract_triples_regex(text: str) -> list[tuple[str, str, str, float]]:
    triples: list[tuple[str, str, str, float]] = []
    seen: set[tuple[str, str, str]] = set()
    padded = text + "\n"
    for patterns in (_ZH_PATTERNS, _EN_PATTERNS):
        for rx, pred in patterns:
            for m in rx.finditer(padded):
                subj, obj = _clean(m.group(1)), _clean(m.group(2))
                if len(subj) < 1 or len(obj) < 1:
                    continue
                if subj.lower() in _STOP or obj.lower() in _STOP or subj == obj:
                    continue
                key = (subj, pred, obj)
                if key in seen:
                    continue
                seen.add(key)
                triples.append((subj, pred, obj, 0.6))  # 启发式置信度
    return triples


def _safe_json_array(raw: str):
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else None
    except json.JSONDecodeError:
        return None


def _extract_llm(text: str, *, active_providers, privacy_tier):
    from ..gateway import llm
    msgs = [{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": text[:4000]}]
    raw, is_real = llm.complete_sync(msgs, policy="economy",
                                     privacy_tier=privacy_tier, active_providers=active_providers)
    if not is_real:
        return None                       # 未真实出网 → 上层回退正则
    data = _safe_json_array(raw)
    if data is None:
        return []                          # 出网了但解析失败 → 空三元组 (is_real 仍 True, 由上层判定)
    out = []
    for d in data:
        if not isinstance(d, dict):
            continue
        s, p, o = _clean(d.get("subject", "")), _clean(d.get("predicate", "")), _clean(d.get("object", ""))
        if not s or not o or s == o:
            continue
        try:
            c = min(1.0, max(0.0, float(d.get("confidence", 0.7))))
        except (TypeError, ValueError):
            c = 0.7                         # 经济档模型偶返非数字 confidence (如 "high") → 单元素降级, 不中断整表抽取
        out.append((s, p, o, c, d.get("subject_type", "concept"), d.get("object_type", "concept")))
    return out


def extract_triples(text: str, *, active_providers=None, privacy_tier: str = "local_first"):
    """对外稳定入口。返回 (triples, is_real)。零 key (active_providers 空) → 纯正则 (..., False)。"""
    if active_providers:
        llm_out = _extract_llm(text, active_providers=active_providers, privacy_tier=privacy_tier)
        if llm_out is not None:            # 含空 list: 真实出网即 is_real=True
            return llm_out, True
    return _extract_triples_regex(text), False
