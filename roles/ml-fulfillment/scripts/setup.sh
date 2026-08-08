#!/bin/bash
# ============================================================
# setup.sh — ML 卖家角色初始化（薄封装）
# 核心逻辑已迁移到 init-ml.sh（同目录），本脚本保持向后兼容：
# 老用户执行 setup.sh 等效于执行 init-ml.sh
#
# 用法:
#   setup.sh                 完整初始化（等价 init-ml.sh）
#   setup.sh --refresh-stores   仅刷新店铺选项
#   setup.sh --reconfig      强制重新配置
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INIT_ML="$SCRIPT_DIR/init-ml.sh"

if [ ! -f "$INIT_ML" ]; then
  echo "✗ 未找到 init-ml.sh（$INIT_ML），请确认脚本完整分发" >&2
  exit 1
fi

exec bash "$INIT_ML" "$@"
