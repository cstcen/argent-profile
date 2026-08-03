---
id: ml-ads-analyzer
name: Mercado Ads 广告分析
version: 1.0.0
category: 营销广告
tags: [广告, Mercado Ads, ROAS, ACoS, Product Ads, 关键词竞价]
difficulty: ⭐⭐⭐⭐
requires:
  - ziniao-cli >= 1.0.0
  - mercadolibre-store-auth
triggers:
  - pattern: 广告|Mercado Ads|ACoS|ROAS|花费|竞价|广告分析|product ads|投放
entry: scripts/analyze.js
output:
  format: markdown
  push_to: [any]
---

# Mercado Ads 广告分析

## 目标
分析 Mercado Ads 广告投放效果，提供 Campaña → Grupo de anuncios → Palabra clave 三级下钻数据，识别浪费花费的关键词，给出优化建议。

## 触发生效条件
- 用户询问「广告效果」「ROAS」「ACoS 超标了」
- 用户需要「调整关键词竞价」
- 用户要求「搜索词报告」
- 定时分析（每周一推送周报）

## 执行流程

### 步骤 1：确认广告权限
```bash
ziniao-cli store list --format json
```
检查当前店铺是否已开通 Mercado Ads 且有投放活动。

### 步骤 2：获取广告整体概览
```bash
ziniao-cli page visit --store-id <ID> --url "<Mercado Ads dashboard>" --wait-until networkidle
ziniao-cli page extract --store-id <ID> --mode page --format json
```
提取汇总指标：
- 总花费
- 曝光量（Impresiones）
- 点击量（Clics）
- 点击率（CTR）
- 总销售额
- ROAS（Return on Ad Spend）
- ACoS（Advertising Cost of Sale）
- 活跃 Campaña 数

### 步骤 3：Campaign 层级下钻
列出所有活跃 Campaign，对每个：
```bash
ziniao-cli page visit --store-id <ID> --url "<Campaign detail URL>" --wait-until networkidle
ziniao-cli page extract --store-id <ID> --mode page --format json
```
每个 Campaign 的：花费、销售额、ROAS、状态（Activa/Pausada）。

### 步骤 4：关键词层级分析
对花费最高的 Campaign，下钻到关键词/搜索词：
- 关键词花费排名
- 每次点击成本（CPC）
- 转化率
- 否定关键词建议（花费高但零转化的词）

### 步骤 5：输出广告分析报告
标准 Markdown 格式：
```markdown
# Mercado Ads 广告分析
店铺: 墨西哥站 | 日期: YYYY-MM-DD

## 整体指标
| 指标 | 值 | 环比上周 |
|------|----|----------|
| 总花费 | — | — |
| ROAS | — | — |
| ACoS | — | — |

## Campaign 表现
| 名称 | 花费 | 销售额 | ROAS | 状态 |
|------|------|--------|------|------|
| — | — | — | — | — |

## 🚨 优化建议
1. **[关键词]** CPC ¥XX，零转化 → 添加为否定关键词
2. **[Campaign]** ACoS 超标（>30%）→ 暂停或调整竞价
3. **[搜索词]** 「搜索词A」花费高但表现好 → 提升竞价
```

## 失败处理
| 错误 | 处理 |
|------|------|
| 当前店铺无广告投放 | 输出「未检测到活跃广告投放」 |
| Campaign 详情页需要更多权限 | 提示用户确认 Mercado Ads 后台权限 |
| 关键词数据量过大 | 只分析花费 Top 20 关键词 |

## 注意事项
- Mercado Ads 后台数据存在 1-2 天延迟
- Product Ads 和 Display Ads 的指标口径不同，需分开分析
- ACoS 和 ROAS 的目标值因品类和利润率而异，需用户设定参考线
- 频繁竞价调整可能导致系统学习期重置，建议每周调整一次
