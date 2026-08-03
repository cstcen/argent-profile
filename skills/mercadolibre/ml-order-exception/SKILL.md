---
id: ml-order-exception
name: 美客多订单异常处理
version: 1.0.0
category: 产品运营
tags: [订单异常, 退货, Reclamos, Devoluciones, Mediación, 超时未发货]
difficulty: ⭐⭐⭐
requires:
  - ziniao-cli >= 1.0.0
  - mercadolibre-store-auth
triggers:
  - pattern: 订单异常|超时未发货|退货|纠纷|reclamo|devolución|mediación|买家投诉
entry: scripts/handle.js
output:
  format: markdown
  push_to: [any]
---

# 美客多订单异常处理

## 目标
检测并汇总美客多卖家中待处理的订单异常——超时未发货订单、买家退货请求（Devoluciones）、纠纷调解（Reclamos/Mediación）——输出异常清单和处理建议。

## 触发生效条件
- 用户询问「有没有异常订单」「退货多了」
- 用户要求「处理纠纷」「检查超时发货」
- 用户需要批量联系买家

## 执行流程

### 步骤 1：获取订单异常概览
```bash
ziniao-cli page visit --store-id <ID> --url "<订单管理页面>" --wait-until networkidle
ziniao-cli page extract --store-id <ID> --mode page --format json
```
提取：
- 待处理发货订单数（超时标记）
- 退货申请（Devoluciones）数量
- 纠纷调解（Mediación）中的案例数
- 已完成的退货数

### 步骤 2：逐类处理异常

#### 2a. 超时未发货订单
列出超时订单清单：
- Order ID
- 下单时间
- 超时时长
- 建议处理：立即发货 / 联系买家说明 / 取消订单

#### 2b. 退货（Devoluciones）
提取退货申请详情：
- 退货原因分类（产品损坏/与描述不符/发错货/买家改变主意）
- 退货截止日期
- 建议处理：同意退货 / 部分退款 / 争议申诉

#### 2c. 纠纷调解（Mediación）
提取纠纷详情：
- 纠纷创建时间
- 当前阶段（买家提交证据 / 卖家回复 / 平台仲裁中）
- 截至目前未超过平台回复时限检查
- 建议：准备证据并在截止前回复

### 步骤 3：批量处理辅助
如用户需要，生成并输出联系人模板（西语/葡语）：
```text
Estimado comprador,
Gracias por contactarnos. Lamentamos los inconvenientes con tu pedido #12345.
[处理方案描述]
Si tienes alguna pregunta, no dudes en responder este mensaje.
Saludos cordiales,
[店铺名]
```

### 步骤 4：输出异常处理报告
标准 Markdown 格式，包含：
- 各类型异常数量统计
- 紧急处理项清单（超时 24h+ 的订单）
- 每项的处理状态和建议
- 新增退货/纠纷趋势（本周 vs 上周）

## 失败处理
| 错误 | 处理 |
|------|------|
| 订单页面无异常数据 | 输出「当前无待处理异常」 |
| 纠纷详情页需要额外登录 | 提示用户确认纠纷中心的可访问性 |
| 批量操作超出平台限制 | 建议用户分批处理 |

## 注意事项
- 订单异常处理有时效性窗口，超时可能导致平台自动判决对卖家不利
- 退货和纠纷处理需结合平台政策（美客多对卖家友好度因站点而异）
- 所有联系买家的操作需要用户最终确认后再执行
