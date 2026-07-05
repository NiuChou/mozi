#!/usr/bin/env python3
"""KG 实体消歧阈值标定 (本地 CLI, 零外呼)。真实 BGE-M3 上线后跑, 据输出改 config.kg_dedup_sim_threshold。

用法: MOZI_EMBED_BACKEND=onnx MOZI_EMBED_MODEL_PATH=... \
      backend/.venv/bin/python backend/scripts/calibrate_kg_threshold.py
"""
import json
import pathlib

from mozi_backend.vault import embedder

PAIRS = pathlib.Path(__file__).parent.parent / "tests/data/kg_dedup_pairs.jsonl"


def main() -> None:
    pairs = [json.loads(line) for line in PAIRS.read_text("utf-8").splitlines() if line.strip()]
    scored = [(p["same"], embedder.cosine(embedder.embed(p["a"]), embedder.embed(p["b"]))) for p in pairs]
    best = None
    for i in range(80, 99):
        th = i / 100.0
        tp = sum(1 for same, s in scored if same and s >= th)
        fp = sum(1 for same, s in scored if not same and s >= th)
        fn = sum(1 for same, s in scored if same and s < th)
        prec = tp / (tp + fp) if tp + fp else 1.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        if prec >= 0.95 and (best is None or f1 > best[3]):   # 保守: 宁漏不误并
            best = (th, prec, rec, f1)
        print(f"th={th:.2f} P={prec:.3f} R={rec:.3f} F1={f1:.3f}")
    print("RECOMMEND:", best, "-> 改 config.kg_dedup_sim_threshold")


if __name__ == "__main__":
    main()
