#!/bin/bash
# ML 卖家角色初始化
# 1. 安装 Playwright
python3 -m venv /tmp/pw-venv --clear
/tmp/pw-venv/bin/pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 写凭据模板
cat > ~/.hermes/scripts/fulfillment.env << 'ENV'
FEISHU_BASE_TOKEN=
FEISHU_TABLE_ID=
ML_STORE_ID=
ML_STORE_NAME=
ZINIAO_DL=
ENV
echo "请编辑 ~/.hermes/scripts/fulfillment.env 填入飞书和紫鸟凭据"

# 3. 注册 cron
hermes cron create --name "ML FULL 货件" --schedule "every 5m" \
  --script poll-fulfillment.sh --no-agent --deliver local
