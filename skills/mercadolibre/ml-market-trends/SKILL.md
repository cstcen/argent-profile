---
id: ml-market-trends
name: 美客多市场趋势洞察
version: 1.0.0
category: 选品与开发
tags: [趋势分析, 热销品类, 季节性, 拉美节日, Tendencias]
difficulty: ⭐⭐
requires:
  - ziniao-cli >= 1.0.0
  - mercadolibre-store-auth
triggers:
  - pattern: 什么好卖|热销|趋势|最近流行|tendencias|什么品类好做
entry: scripts/extract.js
output:
  format: markdown
  push_to: [any]
---

# 美客多市场趋势洞察

## 目标
采集 MercadoLibre 各站点首页及 Tendencias（趋势）模块数据，识别热销品类、飙升商品和季节性机会，输出趋势报告供选品决策参考。

## 触发生效条件
- 用户提及「什么好卖」「热销」「趋势」「最近流行」「tendencias」等关键词
- 用户明确指定站点（墨西哥/巴西/阿根廷/智利/哥伦比亚）
- 必须先确认用户已配置 ziniao-cli 且有可用的美客多店铺环境

## 执行流程

### 步骤 1：确认店铺环境
用户可能未指定站点。优先确认可用的美客多店铺：
```bash
ziniao-cli store list --format json
```
从输出中识别目标站点的 store-id。如无可用店铺，引导用户先添加美客多店铺环境。

### 步骤 2：访问趋势页面
```bash
ziniao-cli page visit --store-id <ID> --url "https://www.mercadolibre.com.mx/tendencias" --wait-until networkidle
```
站点 URL 映射：
- 墨西哥：mercadolibre.com.mx
- 巴西：mercadolibre.com.br
- 阿根廷：mercadolibre.com.ar
- 智利：mercadolibre.cl
- 哥伦比亚：mercadolibre.com.co

### 步骤 3：提取趋势数据
```bash
ziniao-cli page extract --store-id <ID> --mode page --format json
```
提取内容包括：趋势商品标题、当前价格、原价（如有折扣）、评价数、销量参考、所在类目路径。

### 步骤 4：按类目聚合分析
对提取的数据做聚合分析：
- 按类目统计上榜商品数量
- 识别价格带分布（低价段 / 中价段 / 高价段）
- 标记当前拉美节日相关趋势（Día de la Madre、Buen Fin、Navidad、Día del Niño、Hot Sale 等）

### 步骤 5：输出趋势报告
输出标准 Markdown 格式报告，包含：
- 站点 + 时间戳
- 当前热门类目 Top 5
- 每个类目的代表商品（标题 + 价格 + 评价数）
- 季节性/节日机会提醒
- 行动建议

## 失败处理
| 错误 | 处理 |
|------|------|
| store list 为空 | 引导用户先配置紫鸟店铺环境 |
| page visit 超时 | 检查网络，重试一次；如仍失败，提示用户手动确认店铺是否在线 |
| page extract 返回空 | 页面结构可能已变更，通知用户选择器需要更新 |
| 未指定站点时 | 默认取墨西哥站（最大站点），输出中注明 |

## 注意事项
- 趋势页面内容随时间和促销活动动态变化，分析结论仅反映当下快照
- 此技能不涉及真实卖家后台登录，仅抓取平台公开页面数据
- 输出中的销量数据为估算值（评价数与曝光的粗略换算），不作为精准决策依据
