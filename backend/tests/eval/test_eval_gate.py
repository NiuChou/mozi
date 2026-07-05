"""eval 基线回退门禁 (unittest discover 捕获): 三类指标 ≥ baseline 下限。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

try:  # ★须先于 harness 触发临时库环境
    from .._helpers import fresh_conn
except ImportError:
    try:
        from tests._helpers import fresh_conn
    except ImportError:
        from _helpers import fresh_conn

from .harness import DATASETS, load_jsonl, run_all  # noqa: E402

BASELINE = Path(__file__).parent / "baseline.json"


class GoldenCorpusTest(unittest.TestCase):
    """守护 M5 修复: 每篇检索 golden 文档须 >1 chunk, 否则 MRR 结构性恒 1.0 失去判别力。"""

    def test_retrieval_docs_multichunk(self) -> None:
        from mozi_backend.vault import chunking
        for case in load_jsonl(DATASETS / "retrieval.jsonl"):
            for d in case["corpus"]:
                n = len(chunking.chunk_text(d["text"]))
                self.assertGreaterEqual(
                    n, 2, f'{case["id"]}/{d["doc"]} 仅 {n} chunk: 文档过短会令 MRR 恒 1.0 无判别力, 须 >120 token')


class EvalGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.actual = run_all(fresh_conn).to_dict()
        cls.baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}

    def _ge(self, key: str) -> None:
        base = self.baseline.get(key)
        if base is None:
            self.skipTest(f"baseline 无 {key}")
        self.assertGreaterEqual(self.actual.get(key, 0.0), base - 1e-6,
                                f"{key} 回退: {self.actual.get(key)} < {base}")

    def test_routing_not_regressed(self):
        for k in ("routing.task_acc", "routing.policy_acc", "routing.model_acc", "routing.sovereign_ok"):
            self._ge(k)

    def test_retrieval_not_regressed(self):
        for k in ("retrieval.recall_at_k", "retrieval.mrr", "retrieval.inject_precision"):
            self._ge(k)

    def test_kg_not_regressed(self):
        for k in ("kg.precision", "kg.recall", "kg.f1"):
            self._ge(k)


if __name__ == "__main__":
    unittest.main()
