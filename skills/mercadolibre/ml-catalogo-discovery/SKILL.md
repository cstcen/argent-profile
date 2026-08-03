---
id: ml-catalogo-discovery
name: 美客多 Catálogo 选品
version: 1.0.0
category: 选品与开发
tags: [Catálogo, 类目分析, 蓝海, 产品机会, 类目树]
difficulty: ⭐⭐⭐⭐
requires:
  - ziniao-cli >= 1.0.0
  - mercadolibre-store-auth
triggers:
  - pattern: 选品|类目|Catálogo|catalogo|蓝海|机会|子类目|什么产品好做
entry: scripts/extract.js
output:
  format: markdown
  push_to: [any]
---

# 美客多 Catálogo 选品

## 目标
遍历美客多目标类目树结构，分析子类目竞争格局，识别 Listing 少但搜索需求大的蓝海子类目，为产品开发提供数据决策依据。

## 触发生效条件
- 用户指定类目或站点（如「墨西哥站 Audio 类目」）
- 用户询问「什么产品有机会」「哪个子类目竞争小」
- 用户提供类目 ID 或类目名称

## 执行流程

### 步骤 1：确认类目结构
如果用户提供了类目名，先搜索定位：
```bash
ziniao-cli page visit --store-id <ID> --url "https://www.mercadolibre.com.mx/categorias" --wait-until networkidle
ziniao-cli page extract --store-id <ID> --mode page --format json
```
提取顶级类目列表，让用户确认目标类目。

### 步骤 2：遍历子类目
逐层深入目标类目：
```bash
ziniao-cli page visit --store-id <ID> --url "<category-URL>" --wait-until networkidle
ziniao-cli page extract --store-id <ID> --mode page --format json
```
对每个子类目提取：
- 子类目名称和 URL
- Listing 总数
- 搜索结果首页主要内容

### 步骤 3：竞争格局分析
对每个子类目记录：
- Listing 数量（粗略估算）
- 品牌集中度：Top 10 Listing 是否来自相同品牌/卖家
- 价格中位数与区间
- Mercado Ads 广告占比
- 平台推荐商品 vs 普通商品比例

### 步骤 4：蓝海标记
标记满足以下条件的子类目为「机会区域」：
- Listing 数量偏少（相对同级类目）
- 品牌集中度低（无巨头垄断）
- 价格区间大（有利润空间）
- 广告竞争不激烈

### 步骤 5：输出类目机会地图
标准 Markdown 格式，包含：
- 类目树路径概览
- 各子类目竞争评分（红海/黄海/蓝海）
- 推荐进入的子类目及理由
- 该类目代表性 Listing 参考
- 建议的 SKU 方向（配图风格、价格带、卖点）

## 失败处理
| 错误 | 处理 |
|------|------|
| 美客多类目树结构变化 | 从当前页面重新提取导航菜单解析类目层次 |
| 子类目无搜索结果 | 列为「空类目」标记并分析原因（新开类目/限制类目） |
| 类目层级过深 | 仅分析到第三级子类目，更深层级单独请求用户确认 |

## 注意事项
- Catálogo 数据量大，逐级遍历费时，建议先确认到二级类目再展开
- 蓝海判断为相对概念，需结合卖家自身供应链优势综合考虑
- 平台 Catálogo 类目会随运营策略调整，分析结果有时效性（建议每季度更新）
