#!/usr/bin/env bash
# 墨子 · 本地 CI —— 唯一 CI 门禁 (不走云端 GitHub Actions)。
# 信创/离线优先: 推送前 pre-push 钩子在本机跑通整条流水线 (lint → 冒烟 → 构建)。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="backend/.venv/bin/python"
RUFF="backend/.venv/bin/ruff"

if [ ! -x "$PY" ]; then
  echo "✗ 未找到 backend/.venv，请先 make bootstrap" >&2
  exit 1
fi

echo "▶ [1/7] 后端 Lint (ruff)"
( cd backend && "../$RUFF" check . ) || { echo "✗ Lint 失败" >&2; exit 1; }

[ -x backend/.venv/bin/coverage ] || { echo "✗ 缺 coverage, 请重跑 make bootstrap" >&2; exit 1; }

echo "▶ [2/7] 后端单元/集成 (unittest + coverage 计量)"
( cd backend && ".venv/bin/coverage" run \
    --source=mozi_backend.gateway.router,mozi_backend.vault.retrieval,mozi_backend.vault.kg \
    -m unittest discover -s tests -p "test_*.py" ) || { echo "✗ 单元测试失败" >&2; exit 1; }

echo "▶ [3/7] coverage 门禁 (router/retrieval/kg 聚合 ≥90%)"
( cd backend && ".venv/bin/coverage" report --fail-under=90 ) || { echo "✗ 覆盖率不足 90%" >&2; exit 1; }

echo "▶ [4/7] 离线 eval 门禁 (路由/检索/KG golden 不回退基线)"
( cd backend && ".venv/bin/python" -m tests.eval.run_eval ) || { echo "✗ eval 回退基线" >&2; exit 1; }

echo "▶ [5/7] 后端冒烟 (端到端闭环)"
( cd backend && ".venv/bin/python" smoke_test.py ) || { echo "✗ 冒烟失败" >&2; exit 1; }

echo "▶ [6/7] 前端单测 (node:test · SSE 解析/归约)"
if [ -d frontend/node_modules ]; then
  ( cd frontend && npm test ) || { echo "✗ 前端单测失败" >&2; exit 1; }
else
  echo "  ⚠ frontend/node_modules 缺失，跳过 (请先 make bootstrap)"
fi

echo "▶ [7/7] 前端构建 (tsc --noEmit + vite build)"
if [ -d frontend/node_modules ]; then
  ( cd frontend && npm run build ) || { echo "✗ 前端构建失败" >&2; exit 1; }
else
  echo "  ⚠ frontend/node_modules 缺失，跳过 (请先 make bootstrap)"
fi

echo ""
echo "✅ 本地 CI 全绿 —— 可安全推送"
