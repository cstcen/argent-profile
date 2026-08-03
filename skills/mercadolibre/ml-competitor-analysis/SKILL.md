---
id: ml-competitor-analysis
name: 美客多竞品分析
version: 1.0.0
category: 选品与开发
tags: [竞品, 价格追踪, Listing 对比, Reputación, 差距分析]
difficulty: ⭐⭐⭐
requires:
  - ziniao-cli >= 1.0.0
  - mercadolibre-store-auth
triggers:
  - pattern: 竞品|竞争对手|competitor|对比|差距|谁在卖|看对手
entry: scripts/extract.js
output:
  format: markdown
  push_to: [any]
---

# 美客多竞品分析

## 目标
识别并分析指定竞品在目标站点上的 Listing 表现、价格策略、评分状况和 Listing 质量，输出可量化的差距对比报告。

## 触发生效条件
- 用户提供竞品 ASIN/Listing URL 或店铺名称
- 用户要求「分析竞品」「看对手数据」
- 用户询问对比维度（价格/评分/图片/描述）

## 执行流程

### 步骤 1：确认目标竞品
如果用户提供了竞品 URL，直接使用。如果只提供了商品名，先搜索定位：
```bash
ziniao-cli page extract --store-id <ID> --mode page --format json
```
从搜索结果中筛选出目标竞品，获取其 Listing URL 和卖家信息。

### 步骤 2：提取竞品 Listing 数据
访问竞品 Listing 页面：
```bash
ziniao-cli page visit --store-id <ID> --url "<竞品URL>" --wait-until networkidle
ziniao-cli page extract --store-id <ID> --mode page --format json
```
提取内容：
- 标题（Título）长度与关键词覆盖
- 价格与促销标记（Descuento）
- 图片数量和质量
- Ficha técnica（技术参数表）完整度
- Reputación 评分与 Opiniones 数量
- 正面评价关键词 / 负面评价关键词
- Preguntas 数量与回复情况
- 广告投放标记（Anuncio）

### 步骤 3：多维度对比
将竞品数据与用户自己的 Listing（如用户提供）做对比矩阵：
| 维度 | 竞品 | 自己 | 差距 |
|------|------|------|------|
| 价格 | — | — | — |
| Reputación | — | — | — |
| 图片数量 | — | — | — |
| 标题长度 | — | — | — |
| 评价数量 | — | — | — |

### 步骤 4：差评分析
提取竞品差评（Opiniones negativas）高频关键词：

### 步骤 5：输出竞品报告
标准 Markdown 格式，包含：
- 竞品基本信息
- 优劣势雷达图指标数据
- 价格追踪建议频率
- 差评洞察与用户痛点
- Listing 优化建议

## 失败处理
| 错误 | 处理 |
|------|------|
| 竞品 URL 无效 | 提示用户确认链接，重新搜索定位 |
| 竞品店铺 Tienda 页受限 | 尝试直接访问 Listing 页获取卖家信息 |
| 图片数据无法提取 | 仅做数量统计，不分析图内容 |

## 注意事项
- 竞品分析结果仅反映抓取时刻的快照，持续追踪需配合定时任务
- 不做商业秘密窃取（如进货渠道、供应商信息），只分析公开页面数据
- 差评分析目的是发现用户痛点和优化方向，不鼓励恶意攻击竞品
