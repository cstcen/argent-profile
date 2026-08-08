#!/bin/bash
set -euo pipefail
# Argent FULL 货件自动化 — 多店铺并行 wrapper（no_agent 模式，路径参数化版）
# 1) 查询飞书 状态=Pending 且 就绪=true 的记录（兼容数组解析）
# 2) 按店铺名称去重，只 spawn stores.json 中存在的店铺（防止表格出现未配置店铺时误跑）
# 3) 为每个店铺 spawn orchestrator（--store-name 过滤，各自日志）；同店互斥由 orchestrator 内部 flock 保证
# 日志: ~/.hermes/fulfillment-logs/run-<日期>-<店名>.log
#
# 路径参数化（客户机器无开发机固定路径）：
#   orchestrator 解析顺序：$FULFILLMENT_ORCH（fulfillment.env 可配置）
#     → 脚本同目录 orchestrator.py
#     → ~/.hermes/profiles/<profile>/roles/ml-fulfillment/scripts/orchestrator.py
#     → ~/Code/argent-profile/...（开发机兜底）
#   stores.json 解析顺序：$STORES_JSON（fulfillment.env 可配置）→ ~/.hermes/scripts/stores.json
ENV_FILE="$HOME/.hermes/scripts/fulfillment.env"
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
fi

LOG_DIR="$HOME/.hermes/fulfillment-logs"
mkdir -p "$LOG_DIR"

# ── orchestrator 路径解析 ──
ORCH="${FULFILLMENT_ORCH:-}"
if [ -z "$ORCH" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for cand in \
    "$SCRIPT_DIR/orchestrator.py" \
    "$HOME/.hermes/profiles/argent/roles/ml-fulfillment/scripts/orchestrator.py" \
    "$HOME/.hermes/profiles/argent-dev/roles/ml-fulfillment/scripts/orchestrator.py" \
    "$HOME/Code/argent-profile/roles/ml-fulfillment/scripts/orchestrator.py" \
    "$HOME/Code/argent-profile/skills/mercadolibre/ml-fulfillment-sop/scripts/fulfillment_orchestrator.py"; do
    [ -f "$cand" ] && ORCH="$cand" && break
  done
fi
if [ -z "$ORCH" ] || [ ! -f "$ORCH" ]; then
  echo "[poll] 未找到 orchestrator.py，请先运行初始化向导（init-ml.sh）或设置 FULFILLMENT_ORCH" >&2
  exit 1
fi

# ── stores.json 路径解析 ──
STORES_JSON="${STORES_JSON:-$HOME/.hermes/scripts/stores.json}"
if [ ! -f "$STORES_JSON" ]; then
  echo "[poll] 未找到 $STORES_JSON，请先运行初始化向导（init-ml.sh）" >&2
  exit 1
fi

PY=/tmp/pw-venv/bin/python3
[ -x "$PY" ] || PY=python3
LARK="$(command -v lark-cli || echo /opt/homebrew/bin/lark-cli)"

# 查询 状态=Pending 的记录（就绪=true 在解析层过滤；状态字段可能返回数组 ["Pending"] 需兼容）
OUT=$( "$LARK" base +record-list \
  --base-token "${FEISHU_BASE_TOKEN:-}" \
  --table-id "${FEISHU_TABLE_ID:-}" \
  --filter-json '{"logic":"and","conditions":[["状态","==","Pending"]]}' \
  --format json 2>/dev/null || true )

# 解析店铺名称（去重 + stores.json 过滤）
STORES=$( printf '%s' "$OUT" | "$PY" -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
    fields = d["data"]["fields"]
    rows = d["data"]["data"]
except Exception:
    sys.exit(0)
try:
    stores_map = json.load(open(os.environ.get("STORES_JSON", os.path.expanduser("~/.hermes/scripts/stores.json"))))
    valid = set(stores_map.keys())
except Exception:
    valid = None
stores = set()
for row in rows:
    f = dict(zip(fields, row))
    if f.get("就绪") is not True:
        continue
    status = f.get("状态")
    statuses = status if isinstance(status, list) else [status]
    if not any("Pending" in str(s) for s in statuses if s is not None):
        continue
    store = str(f.get("店铺名称") or "").strip()
    if not store:
        continue
    if valid is not None and store not in valid:
        print(f"[poll] 跳过未配置店铺: {store}", file=sys.stderr)
        continue
    stores.add(store)
print("\n".join(sorted(stores)))
' || true )

# 每个店铺 spawn orchestrator（nohup + &，脚本立即退出；同店互斥由内部 flock 保证）
if [ -n "$STORES" ]; then
  N=0
  while IFS= read -r store; do
    if [ -n "$store" ]; then
      LOG_FILE="$LOG_DIR/run-$(date '+%F')-${store}.log"
      FULFILLMENT_LOG_FILE="$LOG_FILE" nohup "$PY" "$ORCH" --mode full --allow-write \
        --store-name "$store" >> "$LOG_FILE" 2>&1 &
      N=$((N + 1))
    fi
  done <<< "$STORES"
  echo "[poll] $(date '+%F %T') 已按店铺并行启动 ${N} 个 orchestrator: $(printf '%s' "$STORES" | tr '\n' ' ')"
fi
exit 0
