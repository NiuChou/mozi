"""分块: 按句/段切, 控 token 预算 (§8.3 索引层)。"""
from __future__ import annotations

import re

from .embedder import tokenize

_SENT_SPLIT = re.compile(r"(?<=[。！？!?\.\n])")


def chunk_text(text: str, target_tokens: int = 120, overlap: int = 1) -> list[str]:
    """贪心按句聚合到 target_tokens; 句间 overlap 句保上下文。"""
    text = text.strip()
    if not text:
        return []
    sentences = [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]
    if not sentences:
        return [text]

    chunks: list[str] = []
    cur: list[str] = []
    cur_tokens = 0
    for sent in sentences:
        n = len(tokenize(sent))
        if cur and cur_tokens + n > target_tokens:
            chunks.append(" ".join(cur))
            cur = cur[-overlap:] if overlap else []
            cur_tokens = sum(len(tokenize(s)) for s in cur)
        cur.append(sent)
        cur_tokens += n
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def count_tokens(text: str) -> int:
    return len(tokenize(text))
