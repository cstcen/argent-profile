#!/usr/bin/env bash
# Argent — 问述科技桌面 AI 助手 一键安装（v0.5.0 Profile Distribution）
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ARGENT_HOME="${ARGENT_HOME:-$HOME/.argent}"
ARGENT_VERSION="v0.5.0"
PIP_MIRROR="-i https://pypi.tuna.tsinghua.edu.cn/simple"
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}→${NC} $*"; }
log_ok()    { echo -e "${GREEN}✓${NC} $*"; }
log_error() { echo -e "${RED}✗${NC} $*"; exit 1; }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║    Argent ${ARGENT_VERSION}  安装中…              ║"
echo "║    问述科技 · 桌面 AI 助手                ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. Node.js（Hermes TUI 依赖）───
log_info "检查 Node.js..."
if ! command -v node &>/dev/null; then
    log_info "正在安装 Node.js..."
    if command -v brew &>/dev/null; then
        brew install node 2>/dev/null || true
    elif command -v apt &>/dev/null; then
        curl -fsSL https://deb.nodesource.com/setup_20.x 2>/dev/null | sudo bash 2>/dev/null
        sudo apt install -y nodejs 2>/dev/null
    elif command -v winget &>/dev/null; then
        winget install OpenJS.NodeJS.LTS 2>/dev/null || true
    fi
fi
command -v node &>/dev/null && log_ok "Node.js $(node --version)" || log_error "请手动安装 Node.js"

# ── 2. Python ──
log_info "检查 Python..."
command -v python3 &>/dev/null || log_error "请先安装 Python 3.10+"
log_ok "Python $(python3 --version | cut -d' ' -f2)"

# ── 3. Hermes Agent ──
log_info "安装 Hermes Agent..."
if command -v hermes &>/dev/null; then
    log_ok "Hermes 已安装 ($(hermes --version 2>/dev/null || echo '?'))"
else
    # 优先官方脚本，失败则 pip 镜像
    if curl -fsSL --connect-timeout 10 https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash 2>&1 | tail -3; then
        :
    else
        log_info "GitHub 不可达，使用镜像安装..."
        python3 -m pip install $PIP_MIRROR -q hermes-agent 2>&1 | tail -3
    fi
    command -v hermes &>/dev/null || log_error "Hermes 安装失败，请检查网络"
    log_ok "Hermes Agent 已安装"
fi

# ── 4. 安装 Argent Profile ──
log_info "安装 Argent Profile (${ARGENT_VERSION})..."
ARGENT_REPO="https://github.com/cstcen/argent-profile.git"
if hermes profile list 2>/dev/null | grep -q argent; then
    log_info "Argent Profile 已存在，更新中..."
    hermes profile update argent 2>&1 | tail -3 || true
else
    hermes profile install "$ARGENT_REPO" --alias argent 2>&1 | tail -5 || {
        # 降级：git clone 手动安装
        log_info "Profile install 失败，尝试 git clone..."
        git clone "$ARGENT_REPO" "$HERMES_HOME/profiles/argent" 2>/dev/null || true
    }
fi
log_ok "Argent Profile 已安装"

# ── 5. 安装 argent-cli（setup/login/role 命令）───
log_info "安装 Argent CLI..."
ARGENT_CLI_URL="https://whyshu.com/dl/argent.tar.gz"
python3 -m pip install $PIP_MIRROR --no-build-isolation -q "$ARGENT_CLI_URL" 2>&1 | tail -1
log_ok "Argent CLI 已安装"

# ── 6. 创建 argent 命令 ──
mkdir -p "$ARGENT_HOME/bin"
cat > "$ARGENT_HOME/bin/argent" << 'ARGENTEOF'
#!/usr/bin/env bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ARGENT_HOME="${ARGENT_HOME:-$HOME/.argent}"

case "${1:-chat}" in
  setup)
    exec python3 -m argent_cli.setup "${@:2}"
    ;;
  update|upgrade)
    echo "🔄 正在更新 Argent..."
    hermes profile update argent
    echo "✅ Argent 已更新"
    ;;
  version|--version|-v)
    echo "Argent v0.5.0"
    ;;
  role)
    exec python3 -m argent_cli.roles "${@:2}"
    ;;
  login)
    exec python3 -m argent_cli.login "${@:2}"
    ;;
  balance|whoami|points)
    exec python3 -m argent_cli.cli "${@}"
    ;;
  *)
    exec hermes -p argent --tui "${@}"
    ;;
esac
ARGENTEOF
chmod +x "$ARGENT_HOME/bin/argent"

if [[ ":$PATH:" != *":$ARGENT_HOME/bin:"* ]]; then
    echo "export PATH=\"$ARGENT_HOME/bin:\$PATH\"" >> "$HOME/.bashrc"
fi
log_ok "argent 命令已创建"

# ── 7. 基础配置 ──
mkdir -p "$HERMES_HOME"
if [ ! -f "$HERMES_HOME/config.yaml" ]; then
    cat > "$HERMES_HOME/config.yaml" << 'YAML'
model:
  default: deepseek-v4-pro
  provider: whyshu
display:
  show_reasoning: false
  interface: tui
YAML
fi

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║    Argent ${ARGENT_VERSION}  安装完成!              ║"
echo "╠══════════════════════════════════════════╣"
echo "║    argent setup   配置账号 + 选角色         ║"
echo "║    argent         开始对话                ║"
echo "║    argent update  更新 Argent            ║"
echo "╚══════════════════════════════════════════╝"
echo ""

if [ -t 0 ]; then exec "$SHELL" -l; fi
