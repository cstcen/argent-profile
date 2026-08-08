#!/bin/bash
# ============================================================
# init-ml.sh — ML 卖家角色初始化向导
# 一次配置全部客户侧参数：紫鸟环境 / ziniao apiKey / 飞书表格 /
# 店铺映射（stores.json）/ CDP 端口 / cron 自动化
#
# 用法:
#   init-ml.sh                完整初始化（首次使用）
#   init-ml.sh --refresh-stores   仅刷新店铺选项（重跑 3a-3g，
#                               紫鸟新增/改名环境后同步）
#   init-ml.sh --reconfig     强制重新配置（忽略已有 fulfillment.env）
#
# 产物:
#   ~/.hermes/scripts/stores.json      店铺映射（环境名 → store_id/platform/cdp_port）
#   ~/.hermes/scripts/fulfillment.env  飞书/店铺凭据
#   ~/.hermes/scripts/poll-fulfillment.sh  轮询脚本（从本目录复制）
#   ~/.hermes/cron/ 下注册「ML FULL 货件」任务
# ============================================================
set -euo pipefail

HERMES_SCRIPTS="$HOME/.hermes/scripts"
ENV_FILE="$HERMES_SCRIPTS/fulfillment.env"
STORES_JSON="$HERMES_SCRIPTS/stores.json"
CDP_CACHE="$HERMES_SCRIPTS/cdp-port-map.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLL_SRC="$SCRIPT_DIR/poll-fulfillment.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log_info() { echo -e "${CYAN}→${NC} $*"; }
log_ok()   { echo -e "${GREEN}✓${NC} $*"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $*"; }
log_err()  { echo -e "${RED}✗${NC} $*"; }

REFRESH_STORES=0
FORCE_RECONFIG=0
for arg in "$@"; do
  case "$arg" in
    --refresh-stores) REFRESH_STORES=1 ;;
    --reconfig)       FORCE_RECONFIG=1 ;;
    *) echo "未知参数: ${arg}（支持 --refresh-stores / --reconfig）" >&2; exit 1 ;;
  esac
done

# ── 工具函数 ──────────────────────────────────────────────
discover_cdp_ports() {
  # 从 ziniaobro 进程发现所有 CDP 调试端口（与 orchestrator._discover_cdp_ports 一致）
  lsof -i -P -n 2>/dev/null | awk '/ziniaobro/ && /LISTEN/ { for (i=1;i<=NF;i++) if ($i ~ /^127\.0\.0\.1:[0-9]+$/) { split($i, a, ":"); print a[2] } }' | sort -n | uniq
}

has_api_key() {
  # 检测紫鸟 apiKey 是否已配置（~/.ziniao/config.json 或 ~/.ziniao-cli/config.json 或 CLI 状态）
  if [ -f "$HOME/.ziniao/config.json" ] && grep -q '"apiKey"' "$HOME/.ziniao/config.json" 2>/dev/null; then
    return 0
  fi
  if [ -f "$HOME/.ziniao-cli/config.json" ] && grep -q '"apiKey"' "$HOME/.ziniao-cli/config.json" 2>/dev/null; then
    return 0
  fi
  if command -v ziniao-cli >/dev/null 2>&1; then
    if ziniao-cli config show 2>/dev/null | grep -q "apiKey"; then
      return 0
    fi
  fi
  return 1
}

# ── 1. 环境检测 ───────────────────────────────────────────
check_env() {
  echo ""
  echo "════════════════════════════════════════════════"
  echo "  🔧 环境检测"
  echo "════════════════════════════════════════════════"

  if command -v ziniao-cli >/dev/null 2>&1; then
    log_ok "ziniao-cli 可用: $(command -v ziniao-cli)"
  else
    log_err "未找到 ziniao-cli"
    echo "  请先安装紫鸟 CLI："
    echo "    npm install -g ziniao-cli"
    echo "    或参考 https://open.ziniao.com 安装说明"
    return 1
  fi

  local ports
  ports=$(discover_cdp_ports)
  if [ -n "$ports" ]; then
    log_ok "紫鸟客户端运行中，发现 CDP 端口: $(echo "$ports" | tr '\n' ' ')"
  else
    log_warn "未检测到紫鸟浏览器进程（ziniaobro）"
    echo "  请先启动紫鸟客户端并打开至少一个店铺窗口，再继续"
    read -r -p "  按回车继续（或 Ctrl-C 退出）... " _dummy
  fi

  if command -v lark-cli >/dev/null 2>&1; then
    log_ok "lark-cli 可用: $(command -v lark-cli)"
  else
    log_err "未找到 lark-cli（飞书 CLI），请先安装"
    return 1
  fi
  return 0
}

# ── 2. ziniao apiKey 配置 ─────────────────────────────────
ensure_api_key() {
  echo ""
  echo "════════════════════════════════════════════════"
  echo "  🔑 紫鸟开放平台 apiKey"
  echo "════════════════════════════════════════════════"

  if has_api_key; then
    log_ok "apiKey 已配置（$(ziniao-cli config show 2>/dev/null | grep -o '"configPath": *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//' || echo '~/.ziniao-cli/config.json')）"
    return 0
  fi

  log_warn "未检测到紫鸟 apiKey 配置"
  echo "  请登录紫鸟开放平台（https://open.ziniao.com）获取你的 apiKey"
  read -r -p "  请输入 apiKey: " api_key
  api_key="$(echo "$api_key" | tr -d '[:space:]')"
  if [ -z "$api_key" ]; then
    log_err "apiKey 不能为空"
    return 1
  fi

  if command -v ziniao-cli >/dev/null 2>&1; then
    # 参照本机现有方式：config init 写入 ~/.ziniao-cli/config.json（明文，成员账号）
    if ! echo "$api_key" | ziniao-cli config init --api-key-stdin --member --no-keychain --profile ml-seller >/dev/null 2>&1; then
      log_warn "ziniao-cli config init 失败，直接写入 ~/.ziniao/config.json"
      mkdir -p "$HOME/.ziniao"
      cat > "$HOME/.ziniao/config.json" << EOF
{
  "apiKey": "$api_key",
  "serverUrl": "https://sbappstoreapi.ziniao.com"
}
EOF
    fi
    ziniao-cli config use ml-seller >/dev/null 2>&1 || true
  else
    mkdir -p "$HOME/.ziniao"
    cat > "$HOME/.ziniao/config.json" << EOF
{
  "apiKey": "$api_key",
  "serverUrl": "https://sbappstoreapi.ziniao.com"
}
EOF
  fi
  log_ok "apiKey 已保存"
  return 0
}

# ── 3. 店铺初始化（核心）─────────────────────────────────
# 3a. 获取环境列表
fetch_store_list() {
  local out
  out=$(ziniao-cli store list 2>/dev/null) || {
    log_err "ziniao-cli store list 失败，请检查 apiKey 与紫鸟客户端状态"
    return 1
  }
  python3 - "$out" << 'PYEOF'
import json, sys
try:
    d = json.loads(sys.argv[1])
    items = d.get("data", {}).get("items", [])
except Exception:
    sys.exit(1)
# 过滤 MercadoLibre 平台
ml = [i for i in items if "MercadoLibre" in (i.get("platformName") or "")]
for i in ml:
    print(f"{i.get('storeName')}\t{i.get('storeId')}\t{i.get('platformName')}")
PYEOF
}

# 3c/3d. 交互选择 + 生成 stores.json
init_stores() {
  echo ""
  echo "════════════════════════════════════════════════"
  echo "  🏬 店铺初始化（紫鸟环境 → FULL 货件）"
  echo "════════════════════════════════════════════════"

  local list
  list=$(fetch_store_list) || return 1
  if [ -z "$list" ]; then
    log_err "未找到 MercadoLibre 环境（ziniao-cli store list 无结果）"
    return 1
  fi

  echo "  紫鸟中的 MercadoLibre 环境："
  echo ""
  local i=0 name id platform
  while IFS=$'\t' read -r name id platform; do
    [ -z "$name" ] && continue
    i=$((i + 1))
    printf "   [%d] %s  (storeId=%s, %s)\n" "$i" "$name" "$id" "$platform"
  done <<< "$list"
  echo ""

  read -r -p "  选择用于 FULL 货件的环境编号（逗号分隔，如 1,2；回车=全部）: " choice
  choice="$(echo "$choice" | tr -d '[:space:]')"
  if [ -z "$choice" ]; then
    choice=$(seq -s, 1 "$i")
  fi

  # 校验选择
  local selected=""
  local c
  IFS=',' read -ra picks <<< "$choice"
  for c in "${picks[@]}"; do
    if [ "$c" -ge 1 ] 2>/dev/null && [ "$c" -le "$i" ] 2>/dev/null; then
      selected="$selected $c"
    else
      log_warn "忽略无效编号: $c"
    fi
  done
  if [ -z "$selected" ]; then
    log_err "未选择任何环境"
    return 1
  fi

  # 3d. 生成 stores.json（cdp_port 待探测）
  echo ""
  log_info "生成店铺映射 stores.json ..."
  mkdir -p "$HERMES_SCRIPTS"
  local sel_list="$selected"
  python3 - "$STORES_JSON" "$sel_list" << 'PYEOF'
import json, os, subprocess, sys

stores_path, sel_str = sys.argv[1], sys.argv[2]
selected = [int(x) for x in sel_str.split()]

# 重新取 store list 保证名称/id 一致
out = subprocess.run(["ziniao-cli", "store", "list"], capture_output=True, text=True)
items = json.loads(out.stdout)["data"]["items"]
ml = [i for i in items if "MercadoLibre" in (i.get("platformName") or "")]

# 重建而非合并：客户取消选择的环境应从 stores.json 移除（--refresh-stores 同步语义）
old = {}
try:
    with open(stores_path) as f:
        old = json.load(f)
except Exception:
    pass

stores = {}
for idx in selected:
    if idx - 1 >= len(ml):
        continue
    it = ml[idx - 1]
    name = it["storeName"]
    prev = old.get(name, {})
    stores[name] = {
        "store_id": it["storeId"],
        "platform": it.get("platformName", ""),
        "cdp_port": prev.get("cdp_port"),
    }

with open(stores_path, "w") as f:
    json.dump(stores, f, ensure_ascii=False, indent=2)
print("stores.json 已更新:", list(stores.keys()))
PYEOF
  return 0
}

# 3e. 打开窗口 + 探测 CDP 端口
probe_store_ports() {
  echo ""
  log_info "打开所选环境窗口并探测 CDP 端口 ..."
  echo "  （每个环境会打开独立可见窗口，请勿关闭；已打开的窗口将复用）"

  python3 - "$STORES_JSON" "$CDP_CACHE" << 'PYEOF'
import json, os, subprocess, sys, time

stores_path, cache_path = sys.argv[1], sys.argv[2]
with open(stores_path) as f:
    stores = json.load(f)

def discover_ports():
    out = subprocess.run(["lsof", "-i", "-P", "-n"], capture_output=True, text=True, timeout=10)
    ports = []
    for line in out.stdout.splitlines():
        if "ziniaobro" in line and "LISTEN" in line:
            parts = line.split()
            for p in parts:
                if p.startswith("127.0.0.1:") or p.startswith("*:"):
                    port = p.split(":")[-1]
                    if port.isdigit():
                        ports.append(int(port))
    return sorted(set(ports))

# 读缓存（店铺名 → 端口）
cached = {}
try:
    with open(cache_path) as f:
        cached = json.load(f)
except Exception:
    pass

for name, entry in stores.items():
    before = set(discover_ports())
    # 打开窗口（非 headless）；store open 输出可能带 "✓ 店铺已打开" 前缀，需定位 JSON 起点
    try:
        r = subprocess.run(["ziniao-cli", "store", "open", "--name", name],
                           capture_output=True, text=True, timeout=60)
        raw = r.stdout
        start = raw.find("{")
        data = json.loads(raw[start:]) if start >= 0 else {}
        reused = data.get("data", {}).get("reused", False)
    except Exception as e:
        print(f"  ⚠ {name}: store open 失败: {e}")
        continue
    time.sleep(4)
    after = set(discover_ports())
    new_ports = sorted(after - before)
    port = None
    if len(new_ports) == 1:
        port = new_ports[0]
    elif reused and name in cached and cached[name] in after:
        port = cached[name]
    elif len(after) == 1:
        port = list(after)[0]
    elif not new_ports and name in cached and cached[name] in after:
        port = cached[name]

    if port is not None:
        entry["cdp_port"] = int(port)
        print(f"  ✓ {name}: CDP 端口 = {port}{'（复用缓存）' if port in cached.values() and port not in new_ports else ''}")
    else:
        entry["cdp_port"] = None
        print(f"  ⚠ {name}: 无法确定 CDP 端口（当前端口: {sorted(after)}），请手动补充 stores.json cdp_port")

with open(stores_path, "w") as f:
    json.dump(stores, f, ensure_ascii=False, indent=2)

# 更新缓存
for name, entry in stores.items():
    if entry.get("cdp_port"):
        cached[name] = int(entry["cdp_port"])
with open(cache_path, "w") as f:
    json.dump(cached, f, ensure_ascii=False, indent=2)
PYEOF
  return 0
}

# 3f. 飞书「店铺名称」字段同步为单选
sync_feishu_field() {
  echo ""
  log_info "同步飞书「店铺名称」字段为单选选项 ..."
  local base_token="$1" table_id="$2"

  python3 - "$STORES_JSON" "$base_token" "$table_id" << 'PYEOF'
import json, subprocess, sys

stores_path, base_token, table_id = sys.argv[1], sys.argv[2], sys.argv[3]
with open(stores_path) as f:
    stores = json.load(f)
names = list(stores.keys())
if not names:
    print("  ⚠ stores.json 为空，跳过字段同步")
    sys.exit(0)

options = [{"name": n} for n in names]
# lark-cli 1.0.53 API 要求字符串判别值 type="select"，options 在顶层
# （"type":3 数字形式会被 API 拒绝: Invalid discriminator value）
field_json = json.dumps({
    "name": "店铺名称",
    "type": "select",
    "multiple": False,
    "options": options,
}, ensure_ascii=False)

r = subprocess.run(
    ["lark-cli", "base", "+field-update",
     "--base-token", base_token, "--table-id", table_id,
     "--field-id", "店铺名称", "--json", field_json, "--yes"],
    capture_output=True, text=True, timeout=60,
)
if r.returncode != 0:
    print(f"  ✗ 字段更新失败: {r.stderr or r.stdout}")
    sys.exit(1)
print(f"  ✓ 「店铺名称」已改为单选，选项: {names}")
PYEOF
  return $?
}

# 3g. 旧记录迁移
migrate_old_records() {
  echo ""
  log_info "检查旧记录「店铺名称」值是否需要迁移 ..."
  local base_token="$1" table_id="$2"

  # 第一步：查询并输出迁移计划（old_value \t target \t record_id,...）
  # 注意：python 通过 heredoc 运行，stdin 被占用，交互确认必须在 bash 层做
  local plan
  plan=$(python3 - "$STORES_JSON" "$base_token" "$table_id" << 'PYEOF'
import json, subprocess, sys

stores_path, base_token, table_id = sys.argv[1], sys.argv[2], sys.argv[3]
with open(stores_path) as f:
    stores = json.load(f)
valid = set(stores.keys())

r = subprocess.run(
    ["lark-cli", "base", "+record-list",
     "--base-token", base_token, "--table-id", table_id,
     "--format", "json"],
    capture_output=True, text=True, timeout=60,
)
if r.returncode != 0:
    print(f"  ⚠ 查询记录失败: {r.stderr or r.stdout}", file=sys.stderr)
    sys.exit(0)
d = json.loads(r.stdout)
fields = d["data"]["fields"]
rows = d["data"]["data"]
rids = d["data"]["record_id_list"]
try:
    col = fields.index("店铺名称")
except ValueError:
    print("  ⚠ 表格无「店铺名称」字段", file=sys.stderr)
    sys.exit(0)

# 旧值 → [record_ids]
old_map = {}
for i, row in enumerate(rows):
    val = row[col] if col < len(row) else None
    if val is None:
        continue
    if isinstance(val, list):
        val = val[0] if val else None
    if val is None or str(val) in valid:
        continue
    old_map.setdefault(str(val), []).append(rids[i])

if not old_map:
    print("  ✓ 无需要迁移的旧记录", file=sys.stderr)
    sys.exit(0)

# 迁移映射（bash 层交互确认）
LEGACY_MAP = {"1店": "1店-子账号", "2店": "2店-子账号", "3店": "3店-主账号"}
for val, ids in old_map.items():
    target = LEGACY_MAP.get(val)
    if target and target in valid:
        print(f"PLAN\t{val}\t{target}\t{','.join(ids)}")
    else:
        print(f"MANUAL\t{val}\t\t{','.join(ids)}")
PYEOF
)
  [ $? -eq 0 ] || return 1

  if [ -z "$plan" ]; then
    return 0
  fi

  # 第二步：bash 交互确认 + 逐条迁移
  # 用 fd 3 读计划，避免 here-string 被内层 read（用户输入）抢走
  local plan_file
  plan_file=$(mktemp)
  printf '%s\n' "$plan" > "$plan_file"
  local failed=0
  while IFS=$'\t' read -r kind val target rid_csv <&3; do
    [ -z "$kind" ] && continue
    if [ "$kind" = "MANUAL" ]; then
      local n_manual
      n_manual=$(echo "$rid_csv" | tr ',' '\n' | grep -c . || true)
      log_warn "「${val}」无法自动映射（${n_manual} 条记录），请人工在飞书表格中处理"
      continue
    fi
    local n
    n=$(echo "$rid_csv" | tr ',' '\n' | grep -c . || true)
    echo "  建议迁移: 「${val}」→「${target}」（$n 条）"
    read -r -p "  确认迁移? [Y/n] " ans
    case "$(echo "$ans" | tr '[:upper:]' '[:lower:]')" in
      ""|y|yes)
        local ok=1 rid
        IFS=',' read -ra rid_arr <<< "$rid_csv"
        for rid in "${rid_arr[@]}"; do
          upd=$(lark-cli base +record-upsert \
            --base-token "$base_token" --table-id "$table_id" \
            --record-id "$rid" --json "{\"店铺名称\":\"$target\"}" 2>&1) || { ok=0; echo "    ✗ 更新 $rid 失败: $upd"; }
        done
        if [ "$ok" = "1" ]; then
          echo "    ✓ 已迁移 $n 条 →「${target}」"
        else
          failed=1
        fi
        ;;
      *)
        echo "    - 跳过「${val}」"
        ;;
    esac
  done 3< "$plan_file"
  rm -f "$plan_file"
  return "$failed"
}

# 3h. 一致性验证
verify_consistency() {
  echo ""
  log_info "验证 stores.json 与表格选项一致性 ..."
  local base_token="$1" table_id="$2"

  python3 - "$STORES_JSON" "$base_token" "$table_id" << 'PYEOF'
import json, subprocess, sys

stores_path, base_token, table_id = sys.argv[1], sys.argv[2], sys.argv[3]
with open(stores_path) as f:
    stores = json.load(f)
store_keys = set(stores.keys())

r = subprocess.run(
    ["lark-cli", "base", "+field-list",
     "--base-token", base_token, "--table-id", table_id],
    capture_output=True, text=True, timeout=60,
)
if r.returncode != 0:
    print(f"  ✗ 查询字段失败: {r.stderr or r.stdout}")
    sys.exit(1)
d = json.loads(r.stdout)
target = None
for fld in d["data"]["fields"]:
    if fld.get("name") == "店铺名称":
        target = fld
        break
if target is None:
    print("  ✗ 未找到「店铺名称」字段")
    sys.exit(1)

if target.get("type") != "select":
    print(f"  ✗ 「店铺名称」字段类型不是单选（当前: {target.get('type')}）")
    sys.exit(1)

opts = {(o.get("name") if isinstance(o, dict) else o) for o in (target.get("options") or [])}
if opts == store_keys:
    print(f"  ✓ 一致性通过: 表格选项 == stores.json keys（{sorted(store_keys)}）")
else:
    print(f"  ✗ 不一致!")
    print(f"    表格选项: {sorted(opts)}")
    print(f"    stores.json: {sorted(store_keys)}")
    print("    请重新运行 --refresh-stores 同步")
    sys.exit(1)
PYEOF
  return $?
}

# ── 4. 飞书配置 ──────────────────────────────────────────
# 4a. 登录态检测
check_lark_login() {
  echo ""
  echo "════════════════════════════════════════════════"
  echo "  📋 飞书配置"
  echo "════════════════════════════════════════════════"

  if lark-cli auth status 2>/dev/null | grep -q '"bot".*"ready"\|"status": "ready"'; then
    log_ok "lark-cli 已登录（bot identity ready）"
  else
    log_warn "lark-cli 未登录或 bot 不可用"
    echo "  请先执行: lark-cli auth login"
    read -r -p "  登录完成后按回车继续... " _dummy
  fi
}

# 4b. 表格 URL 输入解析
parse_table_url() {
  echo ""
  if [ -n "${FEISHU_BASE_TOKEN:-}" ] && [ -n "${FEISHU_TABLE_ID:-}" ] && [ "$FORCE_RECONFIG" = "0" ]; then
    echo "  当前配置表格:"
    echo "    Base Token: ${FEISHU_BASE_TOKEN:0:12}..."
    echo "    Table ID:   ${FEISHU_TABLE_ID}"
    read -r -p "  使用已有表格? [Y/n] " use_existing
    case "$(echo "$use_existing" | tr '[:upper:]' '[:lower:]')" in
      ""|y|yes) return 0 ;;
    esac
  fi
  read -r -p "  请输入飞书多维表格 URL（https://xxx.feishu.cn/base/XXX?table=XXX）: " table_url
  local parsed
  parsed=$(echo "$table_url" | python3 -c "
import re, sys
u = sys.stdin.read().strip()
m1 = re.search(r'/base/([A-Za-z0-9]+)', u)
m2 = re.search(r'table=([A-Za-z0-9]+)', u)
print((m1[1] if m1 else '') + '\t' + (m2[1] if m2 else ''))
")
  FEISHU_BASE_TOKEN=$(echo "$parsed" | cut -f1)
  FEISHU_TABLE_ID=$(echo "$parsed" | cut -f2)
  if [ -z "$FEISHU_BASE_TOKEN" ] || [ -z "$FEISHU_TABLE_ID" ]; then
    log_err "无法解析 URL，请确认格式: https://xxx.feishu.cn/base/XXX?table=XXX"
    return 1
  fi
  log_ok "已解析表格配置"
  return 0
}

# 4c. FEISHU_USER_ID 自动获取
auto_feishu_user() {
  echo ""
  log_info "获取飞书当前用户 open_id（推送目标）..."
  FEISHU_USER_ID=$(lark-cli auth status 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    u = d.get('identities', {}).get('user', {})
    oid = u.get('openId') or u.get('user_open_id')
    if oid:
        print(oid)
except Exception:
    pass
")
  if [ -z "$FEISHU_USER_ID" ]; then
    FEISHU_USER_ID=$(lark-cli contact +get-user --as user 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('user_open_id') or d.get('data', {}).get('open_id') or '')
except Exception:
    pass
")
  fi
  if [ -n "$FEISHU_USER_ID" ]; then
    log_ok "推送目标 open_id: ${FEISHU_USER_ID:0:12}..."
  else
    log_warn "无法自动获取 open_id"
    read -r -p "  请手动输入飞书用户 open_id（或留空跳过）: " FEISHU_USER_ID
  fi
  return 0
}

# ── 5. 机器环境 ──────────────────────────────────────────
setup_machine() {
  echo ""
  echo "════════════════════════════════════════════════"
  echo "  🖥 机器环境（Playwright / 轮询脚本 / cron）"
  echo "════════════════════════════════════════════════"

  # 5a. Playwright venv
  log_info "安装 Playwright（/tmp/pw-venv，python3.11+）..."
  local py
  if command -v python3.11 >/dev/null 2>&1; then py=python3.11
  elif command -v python3.12 >/dev/null 2>&1; then py=python3.12
  elif /usr/bin/python3 --version 2>&1 | grep -q "3.1[1-9]"; then py=/usr/bin/python3
  else py=python3; fi
  "$py" -m venv /tmp/pw-venv --clear 2>/dev/null || true
  /tmp/pw-venv/bin/pip install --quiet playwright -i https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null || \
    /tmp/pw-venv/bin/pip install --quiet playwright 2>/dev/null || true
  if /tmp/pw-venv/bin/python3 -c "import playwright" 2>/dev/null; then
    log_ok "Playwright 就绪（${py}）"
  else
    log_warn "Playwright 安装未确认，请稍后手动执行: /tmp/pw-venv/bin/pip install playwright"
  fi

  # 5b. poll-fulfillment.sh 复制
  if [ -f "$POLL_SRC" ]; then
    cp "$POLL_SRC" "$HERMES_SCRIPTS/poll-fulfillment.sh"
    chmod +x "$HERMES_SCRIPTS/poll-fulfillment.sh"
    log_ok "poll-fulfillment.sh 已复制到 $HERMES_SCRIPTS/"
  else
    log_warn "未找到 poll-fulfillment.sh 源文件（${POLL_SRC}），跳过复制"
  fi

  # 5c. cron 注册
  if hermes cron list 2>/dev/null | grep -q "ML FULL\|poll-fulfillment"; then
    log_ok "Cron 任务已存在，跳过注册"
  else
    log_info "注册自动化任务（每 5 分钟轮询）..."
    if hermes cron create "every 5m" --name "ML FULL 货件" \
      --script poll-fulfillment.sh --no-agent --deliver local 2>/dev/null; then
      log_ok "Cron 任务已注册"
    else
      log_warn "cron 注册失败（可稍后手动执行: hermes cron create \"every 5m\" --name \"ML FULL 货件\" --script poll-fulfillment.sh --no-agent --deliver local）"
    fi
  fi
  return 0
}

# ── 6. 写 fulfillment.env ────────────────────────────────
write_env() {
  echo ""
  log_info "写入 $ENV_FILE ..."
  mkdir -p "$HERMES_SCRIPTS"

  # ML_STORE_* 取客户选择的第一个环境
  local parsed first_store first_id
  parsed=$(python3 -c "
import json
try:
    with open('$STORES_JSON') as f:
        stores = json.load(f)
    k = list(stores.keys())[0]
    print(k + '\t' + str(stores[k].get('store_id', '')))
except Exception:
    print('')
")
  first_store=$(echo "$parsed" | cut -f1)
  first_id=$(echo "$parsed" | cut -f2)

  # ZINIAO_DL 兼容旧 setup.sh：取第一个环境的下载目录（orchestrator 兜底用）
  local ziniaodl
  ziniaodl=$(ziniao-cli store open --name "$first_store" 2>/dev/null | python3 -c "
import json, sys
raw = sys.stdin.read()
start = raw.find('{')
try:
    d = json.loads(raw[start:]) if start >= 0 else {}
    print(d.get('data', {}).get('downloadFolderPath', ''))
except Exception:
    print('')
")

  cat > "$ENV_FILE" << EOF
# FULL 货件编排本地配置（勿提交到仓库）
# 由 init-ml.sh 生成；orchestrator 在环境变量缺失时读取
# 注意：含空格的值必须加引号（bash source 与 Python load_config 均兼容）
FEISHU_BASE_TOKEN=${FEISHU_BASE_TOKEN:-}
FEISHU_TABLE_ID=${FEISHU_TABLE_ID:-}
FEISHU_USER_ID=${FEISHU_USER_ID:-}
ML_STORE_ID=${first_id:-}
ML_STORE_NAME=${first_store:-}
ZINIAO_DL="${ziniaodl:-}"
STORES_JSON=$STORES_JSON
FULFILLMENT_ORCH=${FULFILLMENT_ORCH:-}
EOF
  log_ok "配置已保存"
  return 0
}

# ── 7. 连通性校验（汇总）────────────────────────────────
verify_all() {
  echo ""
  echo "════════════════════════════════════════════════"
  echo "  🔍 连通性校验（汇总）"
  echo "════════════════════════════════════════════════"
  local base_token="${FEISHU_BASE_TOKEN:-}" table_id="${FEISHU_TABLE_ID:-}"
  local fail=0

  # 7a. lark-cli 查表格
  echo ""
  log_info "[1/4] 飞书表格连通性 ..."
  if [ -n "$base_token" ] && [ -n "$table_id" ] && \
     lark-cli base +record-list --base-token "$base_token" --table-id "$table_id" --format json >/dev/null 2>&1; then
    log_ok "表格可查询"
  else
    log_err "表格查询失败（检查 token/table_id 与网络）"
    fail=1
  fi

  # 7b. CDP 可连接
  echo ""
  log_info "[2/4] 紫鸟窗口 CDP 可连接性 ..."
  if [ -f "$STORES_JSON" ]; then
    python3 - "$STORES_JSON" << 'PYEOF'
import json, sys, urllib.request
with open(sys.argv[1]) as f:
    stores = json.load(f)
ok = True
for name, entry in stores.items():
    port = entry.get("cdp_port")
    if not port:
        print(f"  ⚠ {name}: 未配置 cdp_port")
        ok = False
        continue
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            v = json.loads(r.read())
            print(f"  ✓ {name}: 端口 {port} 可连接（{v.get('Browser', '')[:40]}）")
    except Exception as e:
        print(f"  ✗ {name}: 端口 {port} 连接失败: {e}")
        ok = False
sys.exit(0 if ok else 1)
PYEOF
    [ $? -eq 0 ] || fail=1
  else
    log_warn "stores.json 不存在，跳过 CDP 检查"
  fi

  # 7c. stores.json 与表格选项一致
  echo ""
  log_info "[3/4] 店铺映射一致性 ..."
  if [ -n "$base_token" ] && [ -n "$table_id" ] && [ -f "$STORES_JSON" ]; then
    verify_consistency "$base_token" "$table_id" || fail=1
  else
    log_warn "跳过（缺少表格配置或 stores.json）"
  fi

  # 7d. 配置摘要
  echo ""
  log_info "[4/4] 配置摘要"
  echo ""
  echo "  ┌─ 紫鸟环境 ─────────────────────────────"
  if [ -f "$STORES_JSON" ]; then
    python3 - "$STORES_JSON" << 'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    stores = json.load(f)
for name, e in stores.items():
    print(f"  │  {name}  (store_id={e.get('store_id')}, cdp_port={e.get('cdp_port')})")
PYEOF
  fi
  echo "  ├─ 飞书表格 ─────────────────────────────"
  echo "  │  Base: ${base_token:0:12}...  Table: ${table_id}"
  echo "  │  推送目标: ${FEISHU_USER_ID:0:12}..."
  echo "  ├─ cron ────────────────────────────────"
  if hermes cron list 2>/dev/null | grep -q "ML FULL\|poll-fulfillment"; then
    echo "  │  状态: 已注册（每 5 分钟轮询）"
  else
    echo "  │  状态: 未注册"
  fi
  echo "  └────────────────────────────────────────"
  echo ""

  if [ "$fail" = "0" ]; then
    log_ok "全部校验通过 🎉"
  else
    log_warn "存在校验失败项，请按上方提示修复后重试"
  fi
  return "$fail"
}

# ── 主流程 ──────────────────────────────────────────────
main() {
  echo ""
  echo "╔══════════════════════════════════════════════╗"
  echo "║   ML 卖家 — FULL 货件自动化初始化向导         ║"
  echo "║   Argent · 一次配置全部客户参数                ║"
  echo "╚══════════════════════════════════════════════╝"
  echo ""

  # 老用户兼容：已有 fulfillment.env → 跳过重配直接校验
  if [ -f "$ENV_FILE" ] && [ "$FORCE_RECONFIG" = "0" ] && [ "$REFRESH_STORES" = "0" ]; then
    set -a; source "$ENV_FILE"; set +a
    if [ -n "${FEISHU_BASE_TOKEN:-}" ] && [ -n "${FEISHU_TABLE_ID:-}" ]; then
      echo "  检测到已有配置（${ENV_FILE}）"
      read -r -p "  跳过重新配置，直接进入连通性校验? [Y/n] " skip
      case "$(echo "$skip" | tr '[:upper:]' '[:lower:]')" in
        ""|y|yes)
          verify_all
          return $?
          ;;
      esac
      FORCE_RECONFIG=1
    fi
  fi

  check_env || return 1
  ensure_api_key || return 1

  # 飞书配置前置（3e/3f 需要表格 token）
  check_lark_login
  parse_table_url || return 1
  auto_feishu_user

  init_stores || return 1
  probe_store_ports
  sync_feishu_field "$FEISHU_BASE_TOKEN" "$FEISHU_TABLE_ID" || return 1
  migrate_old_records "$FEISHU_BASE_TOKEN" "$FEISHU_TABLE_ID"
  verify_consistency "$FEISHU_BASE_TOKEN" "$FEISHU_TABLE_ID" || return 1

  setup_machine
  write_env
  verify_all
  return $?
}

# ── --refresh-stores：只重跑 3a-3g ───────────────────────
if [ "$REFRESH_STORES" = "1" ]; then
  echo ""
  echo "🔄 刷新店铺选项模式（--refresh-stores）"
  echo "  仅重新同步: 环境列表 → stores.json → CDP 端口 → 表格单选选项 → 旧记录迁移 → 一致性验证"
  echo ""
  if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
  fi
  if [ -z "${FEISHU_BASE_TOKEN:-}" ] || [ -z "${FEISHU_TABLE_ID:-}" ]; then
    log_err "缺少表格配置（$ENV_FILE 不存在或不完整），请先完整运行 init-ml.sh"
    exit 1
  fi
  check_env || exit 1
  init_stores || exit 1
  probe_store_ports
  sync_feishu_field "$FEISHU_BASE_TOKEN" "$FEISHU_TABLE_ID" || exit 1
  migrate_old_records "$FEISHU_BASE_TOKEN" "$FEISHU_TABLE_ID"
  verify_consistency "$FEISHU_BASE_TOKEN" "$FEISHU_TABLE_ID" || exit 1
  log_ok "店铺选项刷新完成"
  exit 0
fi

main "$@"
