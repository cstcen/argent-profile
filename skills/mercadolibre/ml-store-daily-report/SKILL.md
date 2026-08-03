---
id: ml-store-daily-report
name: 美客多店铺日报
version: 1.1.0
category: 产品运营
tags: [日报, 数据汇总, 定时推送, 多站点]
difficulty: ⭐⭐⭐⭐
requires:
  - ziniao-cli >= 1.0.0
  - mercadolibre-store-auth
  - feishu-config
triggers:
  - pattern: 日报|今日数据|运营报告|daily|今天情况|昨天销售|当日汇总|所有店铺|多店
  - schedule: "0 8 * * *"
entry: scripts/extract.js
output:
  format: markdown
  push_to: [feishu]
push:
  enabled: true
  type: card
  template: daily_report
---

# 美客多店铺日报

## 目标
从美客多卖家后台多个页面采集关键运营指标，自动生成结构化日报，支持定时推送飞书。

## 输入格式

**单店铺**（向后兼容）：
```json
{ "store": "墨西哥站", "orders": {...}, "reputation": {...}, "full": {...} }
```

**多店铺**（v1.1.0）：
```json
{ "stores": [
  { "store": "墨西哥站", "orders": {...}, ... },
  { "store": "巴西站",   "orders": {...}, ... }
]}
```

## 触发生效条件
- 用户每天固定时段要求「生成日报」
- 用户手动触发「今日数据」
- 定时任务（cron）每天 08:00 自动触发

## 执行流程

### 步骤 1：确认店铺清单
```bash
ziniao-cli store list --format json
```
获取所有美客多店铺环境及其对应的站点。记录每个店铺的 store-id。

### 步骤 2：逐店采集数据
对每个活跃店铺循环执行以下采集。如店铺数超过 5 个，输出聚合概览 + 分店详情。

#### 2a. 订单数据
采集今日/昨日订单量、销售额、平均客单价、退货单数。

#### 2b. 在售 Listing
当前在线 Listing 数、下架数、新建草稿数。

#### 2c. 评价与声誉
```bash
ziniao-cli page visit --store-id <ID> --url "<Reputación URL>" --wait-until networkidle
ziniao-cli page extract --store-id <ID> --mode page --format json
```
Reputación 评分变化、新 Opiniones（好评/差评数量）、新 Preguntas 数量。

#### 2d. FULL 库存预警
FULL 仓库库存水平、断货 SKU 数、待处理货件数。

#### 2e. 广告概览
Mercado Ads 今日花费、曝光量、点击率、ACoS（如店铺有广告投放）。

### 步骤 3：生成日报
将以上数据填入标准日报模板，输出 Markdown 报告。
模板结构：
```markdown
# 美客多运营日报
**日期**: YYYY-MM-DD | **店铺**: XX站

## 📦 数据概览
| 指标 | 今日 | 环比昨日 |
|------|------|----------|
| 订单量 | — | — |
| 销售额 | — | — |
| 退货数 | — | — |

## 📊 在售情况
...

## 🌟 声誉动态
...

## ⚠️ 告警项
...
```

### 步骤 4：推送
根据 output.push_to 配置将日报推送到目标渠道。如用户未配置，输出至终端。

## 失败处理
| 错误 | 处理 |
|------|------|
| 部分店铺页面加载失败 | 跳过该店铺，在日报中标明「数据缺失」，不中断其他店铺采集 |
| 全部采集失败 | 输出错误摘要，提示用户检查店铺环境状态 |
| 定时任务未配置 | 提醒用户设置 push_to 和目标渠道 |
| 遇到验证码/登录过期 | 提示用户重新在紫鸟浏览器中登录美客多卖家账号 |

## 注意事项
- 日报品质取决于数据源的可用性——部分指标如果页面未加载完整则采集不到
- 环比昨日对比需要缓存前一天数据（本技能不管理缓存，依赖用户存储上次报告）
- 切忌在日报中包含敏感信息（API Key 等），所有输出均为聚合运营指标
