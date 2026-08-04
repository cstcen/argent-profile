#!/bin/bash
# ML 卖家角色初始化 — 飞书表格配置向导
set -euo pipefail

ENV_FILE="$HOME/.hermes/scripts/fulfillment.env"
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}→${NC} $*"; }
log_ok()    { echo -e "${GREEN}✓${NC} $*"; }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║    ML 卖家 — FULL 货件自动化              ║"
echo "║    飞书多维表格配置向导                    ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. Playwright 环境 ──
log_info "安装 Playwright（CDP 浏览器控制）..."
python3 -m venv /tmp/pw-venv --clear 2>/dev/null
/tmp/pw-venv/bin/pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple -q 2>/dev/null
log_ok "Playwright 就绪"

# ── 2. 飞书表格配置 ──
mkdir -p "$(dirname "$ENV_FILE")"

if [ -f "$ENV_FILE" ]; then
  source "$ENV_FILE"
  if [ -n "${FEISHU_BASE_TOKEN:-}" ] && [ -n "${FEISHU_TABLE_ID:-}" ]; then
    echo ""
    echo "当前已配置表格:"
    echo "  Base Token: ${FEISHU_BASE_TOKEN:0:12}..."
    echo "  Table ID:   ${FEISHU_TABLE_ID}"
    echo ""
    echo "选择操作:"
    echo "  [1] 使用已有（跳过此步）"
    echo "  [2] 新建表格"
    echo "  [3] 输入其他表格 URL"
    read -p "请输入 [1/2/3] (默认 1): " table_choice
    table_choice="${table_choice:-1}"
  else
    table_choice="2"
  fi
else
  table_choice="2"
fi

case "${table_choice}" in
  1)
    log_ok "使用已有表格配置"
    ;;
  2)
    log_info "创建新多维表格..."
    # 创建 Base
    BASE_RESP=$(lark-cli base create --name "FULL 货件管理" 2>&1)
    BASE_TOKEN=$(echo "$BASE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['base_token'])" 2>/dev/null || echo "")
    if [ -z "$BASE_TOKEN" ]; then
      echo "⚠️  自动创建失败，请手动创建后执行选项 3"
    else
      # 创建表格
      TABLE_RESP=$(lark-cli base +table-create --base-token "$BASE_TOKEN" --name "货件记录" 2>&1)
      TABLE_ID=$(echo "$TABLE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['table_id'])" 2>/dev/null || echo "")

      # 创建字段
      for field in '{"field_name":"SKU","type":1}' \
                   '{"field_name":"品名","type":1}' \
                   '{"field_name":"数量","type":1}' \
                   '{"field_name":"箱数","type":1}' \
                   '{"field_name":"状态","type":1}' \
                   '{"field_name":"就绪","type":7}' \
                   '{"field_name":"货件号","type":1}' \
                   '{"field_name":"当前步骤","type":1}' \
                   '{"field_name":"产品标签","type":17}' \
                   '{"field_name":"箱唛","type":17}' \
                   '{"field_name":"店铺名称","type":1}'; do
        lark-cli base +field-create --base-token "$BASE_TOKEN" --table-id "$TABLE_ID" --json "$field" 2>/dev/null || true
      done

      FEISHU_BASE_TOKEN="$BASE_TOKEN"
      FEISHU_TABLE_ID="$TABLE_ID"
      log_ok "表格已创建"
      echo "  Base URL: https://xxx.feishu.cn/base/$BASE_TOKEN"
    fi
    ;;
  3)
    read -p "请输入飞书多维表格 URL: " table_url
    # 从 URL 提取 token 和 table_id
    # 格式: https://xxx.feishu.cn/base/BASE_TOKEN?table=TABLE_ID
    FEISHU_BASE_TOKEN=$(echo "$table_url" | python3 -c "import sys; u=sys.stdin.read().strip(); m=__import__('re').search(r'/base/([A-Za-z0-9]+)',u); print(m[1] if m else '')" 2>/dev/null || echo "")
    FEISHU_TABLE_ID=$(echo "$table_url" | python3 -c "import sys; u=sys.stdin.read().strip(); m=__import__('re').search(r'table=([A-Za-z0-9]+)',u); print(m[1] if m else '')" 2>/dev/null || echo "")
    if [ -n "$FEISHU_BASE_TOKEN" ] && [ -n "$FEISHU_TABLE_ID" ]; then
      log_ok "已解析表格配置"
    else
      echo "⚠️  无法解析 URL，请确认格式: https://xxx.feishu.cn/base/XXX?table=XXX"
    fi
    ;;
esac

# ── 3. 写配置 ──
cat > "$ENV_FILE" << ENVEOF
FEISHU_BASE_TOKEN=${FEISHU_BASE_TOKEN:-}
FEISHU_TABLE_ID=${FEISHU_TABLE_ID:-}
ML_STORE_ID=${ML_STORE_ID:-}
ML_STORE_NAME=${ML_STORE_NAME:-}
ZINIAO_DL=${ZINIAO_DL:-}
ENVEOF
log_ok "配置已保存到 $ENV_FILE"

# ── 4. 注册 Cron ──
if hermes cron list 2>/dev/null | grep -q "ML FULL"; then
  log_ok "Cron 任务已存在"
else
  log_info "注册自动化任务（每 5 分钟）..."
  hermes cron create --name "ML FULL 货件" --schedule "every 5m" \
    --script poll-fulfillment.sh --no-agent --deliver local 2>/dev/null || true
  log_ok "Cron 任务已注册"
fi

echo ""
echo "══════════════════════════════════════════"
echo "  🎉 ML 卖家角色配置完成！"
echo ""
echo "  使用方式："
echo "    在飞书表格填 SKU + 品名 + 数量 + 箱数"
echo "    勾选「就绪」复选框"
echo "    Argent 自动执行，结果回写表格"
echo "══════════════════════════════════════════"
