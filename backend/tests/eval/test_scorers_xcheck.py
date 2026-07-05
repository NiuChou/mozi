"""可选 dev 交叉校验 (复用 C 落地): sklearn/pytrec_eval 在则对照自写 scorers, 缺库自动 skip。

不入 requirements (实证均未装; 引入 scipy/numpy/C 扩展违反轻栈+信创)。权威路径恒为 scorers.py 纯 Python。
"""
from __future__ import annotations

import unittest

from . import scorers

try:
    import sklearn.metrics as skm
    HAS_SK = True
except ImportError:
    HAS_SK = False

try:
    import pytrec_eval
    HAS_PT = True
except ImportError:
    HAS_PT = False


class ScorersXCheckTest(unittest.TestCase):
    @unittest.skipUnless(HAS_SK, "sklearn 未装 (可选 dev 交叉校验, 非门禁)")
    def test_prf1_matches_sklearn(self):
        expected = {("a",), ("b",), ("c",)}
        got = {("a",), ("b",), ("x",)}
        universe = sorted(expected | got)
        y_true = [1 if u in expected else 0 for u in universe]
        y_pred = [1 if u in got else 0 for u in universe]
        p, r, f1 = scorers.prf1(expected, got)
        sp, sr, sf1, _ = skm.precision_recall_fscore_support(
            y_true, y_pred, average="binary", zero_division=0)
        self.assertAlmostEqual(p, sp, places=6)
        self.assertAlmostEqual(r, sr, places=6)
        self.assertAlmostEqual(f1, sf1, places=6)

    @unittest.skipUnless(HAS_PT, "pytrec_eval 未装 (可选)")
    def test_mrr_matches_pytrec(self):
        qrels = {"q1": {"d2": 1}}
        run = {"q1": {"d1": 0.9, "d2": 0.8, "d3": 0.7}}
        ev = pytrec_eval.RelevanceEvaluator(qrels, {"recip_rank"})
        pt_rr = ev.evaluate(run)["q1"]["recip_rank"]
        self.assertAlmostEqual(scorers.reciprocal_rank([False, True, False]), pt_rr, places=6)


if __name__ == "__main__":
    unittest.main()
