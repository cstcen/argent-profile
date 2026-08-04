# ML 卖家角色 — FULL 货件自动化（ml-fulfillment）

让用户通过 Argent Profile Distribution 获得美客多（Mercado Libre）FULL 货件
创建与发货的自动化能力：监听飞书待办记录 → 自动执行 8 步货件创建流程 →
回写结果。

## 能力概述

- 轮询飞书多维表格中「待处理」状态的 FULL 货件记录
- 通过 Playwright CDP 连接紫鸟浏览器（ziniaobro），驱动美客多卖家后台
- 按 SOP 执行 8 步流程：准备检查 → 创建入口 → 选品数量 → 预约时间 →
  包装确认 → 标签下载 → 箱唛打印 → 取消预约
- 步骤 3/4/5/7/8 为写操作，需要用户批准后才执行（`--allow-write` 才放行）
- 执行结果回写飞书（货件 ID、SKU、数量、异常状态），并归档店铺日报

## 目录结构

```
roles/ml-fulfillment/
├── README.md                  # 本文件：角色说明
├── scripts/
│   ├── orchestrator.py        # Python 编排器（Playwright CDP 驱动）
│   ├── fulfillment.js         # 选择器配置（单一事实源 + fallback 链）
│   └── setup.sh               # 一键初始化脚本
├── skills/
│   └── ml-fulfillment-sop/
│       └── SKILL.md           # Agent 技能描述（8 步 SOP + 异常处理）
└── cron/
    └── poll-fulfillment.cron.yaml   # cron 任务定义模板
```

## 前置条件

| 依赖 | 说明 |
|------|------|
| 紫鸟浏览器 | `ziniaobro` 进程（Playwright CDP 自动发现调试端口） |
| ziniao-cli | `ziniao-cli store open` 切换店铺（多店铺 STORE_MAP） |
| 飞书多维表格 | 存放 FULL 货件待办记录（Base Token + Table ID） |
| Python 3.11+ | Playwright 最新版要求（系统 python3.9 不支持） |

## 安装步骤

```bash
cd roles/ml-fulfillment
bash scripts/setup.sh
```

setup.sh 会：

1. 创建 `/tmp/pw-venv` 虚拟环境并安装 Playwright（清华源）
2. 生成凭据模板 `~/.hermes/scripts/fulfillment.env` —— 编辑填入
   飞书和紫鸟凭据（见下方环境变量表）
3. 注册 5 分钟轮询的 cron 任务（no-agent 模式，本地投递）

### 环境变量（fulfillment.env）

| 变量 | 必填 | 说明 |
|------|------|------|
| FEISHU_BASE_TOKEN | 是 | 飞书多维表格 Base Token |
| FEISHU_TABLE_ID | 是 | 飞书多维表格 Table ID |
| ML_STORE_ID | 是 | 默认美客多店铺 ID |
| ML_STORE_NAME | 是 | 默认店铺名称（多店铺时启动对应紫鸟店铺） |
| FEISHU_USER_ID | 否 | 飞书用户 ID（日报归档） |
| ZINIAO_DL | 否 | 紫鸟下载目录，默认 `~/Library/Application Support/ziniaobrowserdatas/ziniao browser` |

### cron 轮询脚本

setup.sh 注册的 cron 指向 `poll-fulfillment.sh`（放在
`~/.hermes/scripts/` 下），它是极简 wrapper：加载 `fulfillment.env` 后
exec 编排器：

```bash
#!/bin/bash
set -euo pipefail
ENV_FILE="$HOME/.hermes/scripts/fulfillment.env"
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
fi
exec /tmp/pw-venv/bin/python3 \
  "$HOME/Code/argent-profile/roles/ml-fulfillment/scripts/orchestrator.py" \
  --mode full --allow-write
```

> 注意：`--allow-write` 表示 cron 自动执行写步骤。若希望写操作等待人工
> 批准，去掉该参数（编排器会输出 needs_approval 状态）。

## 手动验证

```bash
# 编排器自检（无副作用：加载选择器、检查配置完整性）
/tmp/pw-venv/bin/python3 roles/ml-fulfillment/scripts/orchestrator.py --inspect

# dry-run（连接飞书，报告当前待处理记录，不执行写操作）
/tmp/pw-venv/bin/python3 roles/ml-fulfillment/scripts/orchestrator.py --mode full
```

## 向后兼容

本角色目录是 `skills/mercadolibre/ml-fulfillment-sop/` 的发行副本，
**不修改**原目录，原路径脚本（旧 cron 引用）继续可用。升级流程：
先更新原目录源码，再同步复制到本角色目录。

## 安全与凭据

- 所有凭据只存于本地 `~/.hermes/scripts/fulfillment.env`（gitignored，勿提交）
- 编排器不包含任何硬编码凭据
- cron 模板不含真实密钥，占位符统一 `<PLACEHOLDER>` 形式
