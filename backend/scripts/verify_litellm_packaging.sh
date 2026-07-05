#!/usr/bin/env bash
# 墨子 · litellm 桌面打包体积 / 离线可重现性验收 (ADAPTER epic 真实切换底座时跑)。
# 本期 (METER) 仅落约束清单 + 验证脚本; litellm 调用代码本体归 ADAPTER。
set -euo pipefail

THRESHOLD_MB="${MOZI_LITELLM_SIZE_THRESHOLD_MB:-80}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "▶ [1/3] 体积评估 (仅核心 litellm, 禁 [proxy], --no-deps 量化增量)"
python -m pip download "litellm" --no-deps -d "$WORK" >/dev/null 2>&1 || {
  echo "  ⚠ pip download 失败 (离线/无 litellm 发布?) — 体积评估跳过"; }
SIZE_MB=$(du -sm "$WORK" 2>/dev/null | cut -f1 || echo 0)
echo "  litellm 核心 wheel 增量 ≈ ${SIZE_MB}MB (阈值 ${THRESHOLD_MB}MB)"
if [ "${SIZE_MB:-0}" -gt "$THRESHOLD_MB" ]; then
  echo "  ⚠ 超阈值: 评估是否仅 vendoring litellm.acompletion 路径" >&2
fi

echo "▶ [2/3] 离线可重现性 (要求 requirements.lock 带 --require-hashes)"
if [ -f backend/requirements.lock ]; then
  python -m pip install --require-hashes -r backend/requirements.lock --dry-run \
    || { echo "✗ 哈希锁安装校验失败" >&2; exit 1; }
else
  echo "  ⚠ backend/requirements.lock 缺失 (ADAPTER landing 时用 pip-compile --generate-hashes 生成)"
fi

echo "▶ [3/3] import 自检 + 强制静态价表 (禁触网拉价)"
LITELLM_LOCAL_MODEL_COST_MAP=True python -c "import litellm; print('litellm', litellm.__version__)" \
  || echo "  ⚠ litellm 未安装 (本期可选; 缺失时墨子降级 mock/llama-local)"

echo "✅ litellm 打包验收完成 (阈值=${THRESHOLD_MB}MB)"
