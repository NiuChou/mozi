"""离线 eval CLI: python -m tests.eval.run_eval [--update-baseline]。退出码 0 过基线 / 1 回退 / 2 数据集缺失。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:  # ★须先于 harness (其 import mozi_backend) 触发临时库环境
    from .._helpers import fresh_conn
except ImportError:
    try:
        from tests._helpers import fresh_conn
    except ImportError:
        from _helpers import fresh_conn

from .harness import run_all  # noqa: E402

BASELINE = Path(__file__).parent / "baseline.json"


def _print_table(d: dict) -> None:
    print("=== eval 报告 ===")
    for k in sorted(d):
        print(f"  {k:32s} {d[k]:.4f}")


def main(argv: list[str]) -> int:
    try:
        report = run_all(fresh_conn)
    except FileNotFoundError as e:
        print(f"✗ 数据集缺失: {e}", file=sys.stderr)
        return 2
    d = report.to_dict()
    if "--update-baseline" in argv:
        BASELINE.write_text(json.dumps({k: round(v, 4) for k, v in d.items()},
                                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"✅ 基线已更新 → {BASELINE}")
        _print_table(d)
        return 0
    _print_table(d)
    if not BASELINE.exists():
        print("⚠ 无基线, 先跑 --update-baseline", file=sys.stderr)
        return 0
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    regressions = [(k, d.get(k, 0.0), v) for k, v in baseline.items() if d.get(k, 0.0) < v - 1e-6]
    for k in baseline:
        if k not in d:
            print(f"⚠ 指标 {k} 缺失 (放行)", file=sys.stderr)
    if regressions:
        for k, got, base in regressions:
            print(f"✗ 回退 {k}: {got:.4f} < 基线 {base:.4f}", file=sys.stderr)
        return 1
    print("✅ eval 全过基线")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
