---
id: ml-keyword-research
name: 美客多关键词调研
version: 1.0.0
category: 选品与开发
tags: [关键词, 搜索分析, 竞争度, SEO, 搜索词]
difficulty: ⭐⭐⭐
requires:
  - ziniao-cli >= 1.0.0
  - mercadolibre-store-auth
triggers:
  - pattern: 关键词|搜索词|keyword|竞争度|搜索量|audífonos|什么词
entry: scripts/extract.js
output:
  format: markdown
  push_to: [any]
---

# 美客多关键词调研

## 目标
通过美客多搜索建议自动补全和搜索结果页分析，评估目标关键词的搜索热度、竞争强度和价格区间，为 Listing 优化和广告投放提供数据支撑。

## 触发生效条件
- 用户提供具体关键词（西班牙语优先）和目标站点
- 用户询问「什么词好做」「搜索量大不大」
- 需事先确认店铺环境可用

## 执行流程

### 步骤 1：确认店铺与站点
```bash
ziniao-cli store list --format json
```
确认目标站点对应的 store-id。如果用户未指定站点，默认使用墨西哥站。

### 步骤 2：获取搜索建议词
访问美客多搜索页面，利用自动补全接口提取关联关键词：
```bash
ziniao-cli page visit --store-id <ID> --url "https://www.mercadolibre.com.mx/" --wait-until networkidle
```
在搜索框中输入关键词前缀，提取下拉建议词列表。记录所有关联词。

### 步骤 3：搜索结果页分析
对核心关键词和关联词分别执行搜索：
```bash
ziniao-cli page visit --store-id <ID> --url "https://www.mercadolibre.com.mx/audifonos-bluetooth#menu=trend" --wait-until networkidle
ziniao-cli page extract --store-id <ID> --mode page --format json
```
提取内容：
- 搜索结果总数（页面标题或统计栏）
- Listing 标题列表
- 价格分布（最低价、最高价、中位数）
- 评价分布（0-10 条 / 10-100 条 / 100+ 条）
- 搜索结果页的广告商品数

### 步骤 4：评估竞争度
综合以下指标输出竞争度评分（低/中/高）：
- Listing 数量：少（<500）→ 蓝海机会
- 大卖占比：前 10 名中 Reputación 评分高的卖家数量
- 价格集中度：是否集中在狭窄区间（价格战风险）
- 广告密度：搜索结果首页广告占比

### 步骤 5：输出关键词报告
标准 Markdown 格式，包含：
- 核心关键词 + 站点 + 时间
- 关联词列表（5-10 个）
- 竞争度评级
- 价格区间分析
- 优化建议（推荐长尾词、否定词建议）

## 失败处理
| 错误 | 处理 |
|------|------|
| 搜索词包含非西班牙语字符 | 建议用户使用西班牙语关键词（美客多核心市场为西语/葡语区） |
| 搜索结果页结构异常 | UI 可能更新，回退到基础提取策略 |
| 自动补全不返回结果 | 尝试较短的关键词前缀 |

## 注意事项
- 美客多不提供公开的搜索量数据，所有热度评估基于搜索结果数量和广告密度的间接推断
- 巴西站点（.com.br）使用葡萄牙语关键词
- 关键词调研结果用于 Listing 标题优化和 Product Ads 关键词选择
