"""BM25 稀疏检索 (手写, 零依赖)。对齐 §8.3 索引层 BM25 路。"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from .embedder import tokenize


@dataclass
class BM25:
    k1: float = 1.5
    b: float = 0.75

    def __post_init__(self) -> None:
        self.docs: list[list[str]] = []
        self.ids: list[str] = []
        self.df: Counter[str] = Counter()
        self.avgdl: float = 0.0

    def index(self, items: list[tuple[str, str]]) -> None:
        """items: [(doc_id, text)]"""
        self.docs = []
        self.ids = []
        self.df = Counter()
        for doc_id, text in items:
            toks = tokenize(text)
            self.docs.append(toks)
            self.ids.append(doc_id)
            for term in set(toks):
                self.df[term] += 1
        total = sum(len(d) for d in self.docs)
        self.avgdl = total / len(self.docs) if self.docs else 0.0

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        if not self.docs:
            return []
        q_terms = tokenize(query)
        n = len(self.docs)
        scores: list[tuple[str, float]] = []
        for i, doc in enumerate(self.docs):
            dl = len(doc)
            tf = Counter(doc)
            score = 0.0
            for term in q_terms:
                if term not in tf:
                    continue
                idf = math.log(1 + (n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                num = tf[term] * (self.k1 + 1)
                den = tf[term] + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                score += idf * num / den
            if score > 0:
                scores.append((self.ids[i], score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]
