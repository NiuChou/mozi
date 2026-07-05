"""eval harness: 唯一碰 conn 的层。零外呼, 每检索 case 独立 fresh_conn + FK 前置 ensure_user。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

try:  # ★须先于任何 mozi_backend import: _helpers 在 import 期把 DATA_DIR/DB 指向临时库
    from .._helpers import fresh_conn
except ImportError:
    try:
        from tests._helpers import fresh_conn
    except ImportError:
        from _helpers import fresh_conn

from mozi_backend.db import dal  # noqa: E402
from mozi_backend.gateway.models import MODELS  # noqa: E402
from mozi_backend.gateway.router import RouteRequest, route  # noqa: E402
from mozi_backend.vault import kg, retrieval, service  # noqa: E402

from . import scorers  # noqa: E402

DATASETS = Path(__file__).parent / "datasets"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(json.loads(s))
    return out


def run_routing_eval() -> scorers.RoutingMetrics:
    cases = load_jsonl(DATASETS / "routing.jsonl")
    decisions = [route(RouteRequest(**c["req"])) for c in cases]
    domestic_map = {mid: m.domestic for mid, m in MODELS.items()}
    return scorers.score_routing(cases, decisions, domestic_map)


def run_retrieval_eval(conn_factory=fresh_conn) -> scorers.RetrievalMetrics:
    cases = load_jsonl(DATASETS / "retrieval.jsonl")
    recs, rrs, injects = [], [], []
    for c in cases:
        conn = conn_factory()
        try:
            # FK 前置: archive_document 前必须先建 user 行 (ensure_user(conn,uid,email)); email 无默认值
            dal.ensure_user(conn, "u_eval", "eval@local.cn")
            for d in c["corpus"]:
                service.archive_document(conn, user_id="u_eval", title=d["doc"], content=d["text"])
            res = retrieval.search(conn, "u_eval", c["query"], k=c.get("k", 3))
            hit = [any(t in (h.text or "") for t in c["relevant_contains"]) for h in res.hits]
            recs.append(scorers.recall_at_k(hit))
            rrs.append(scorers.reciprocal_rank(hit))
            injects.append(1.0 if res.injected else 0.0)
        finally:
            conn.close()
    n = len(cases)
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    return scorers.RetrievalMetrics(n, avg(recs), avg(rrs), avg(injects))


def run_kg_eval() -> scorers.KGMetrics:
    cases = load_jsonl(DATASETS / "kg.jsonl")
    tp = fp = fn = 0
    for c in cases:
        triples, _ = kg.extract_triples(c["text"])
        got = {(t[0], t[1], t[2]) for t in triples}
        expected = {tuple(x) for x in c["expect_triples"]}
        tp += len(expected & got)
        fp += len(got - expected)
        fn += len(expected - got)
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return scorers.KGMetrics(len(cases), p, r, f1)


@dataclass
class EvalReport:
    routing: scorers.RoutingMetrics
    retrieval: scorers.RetrievalMetrics
    kg: scorers.KGMetrics

    def to_dict(self) -> dict:
        return {
            "routing.task_acc": self.routing.task_acc,
            "routing.policy_acc": self.routing.policy_acc,
            "routing.model_acc": self.routing.model_acc,
            "routing.sovereign_ok": self.routing.sovereign_ok,
            "retrieval.recall_at_k": self.retrieval.recall_at_k,
            "retrieval.mrr": self.retrieval.mrr,
            "retrieval.inject_precision": self.retrieval.inject_precision,
            "kg.precision": self.kg.precision,
            "kg.recall": self.kg.recall,
            "kg.f1": self.kg.f1,
        }


def run_all(conn_factory=fresh_conn) -> EvalReport:
    return EvalReport(run_routing_eval(), run_retrieval_eval(conn_factory), run_kg_eval())
