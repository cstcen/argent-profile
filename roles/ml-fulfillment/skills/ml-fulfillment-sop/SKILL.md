---
id: ml-fulfillment-sop
name: 美客多 FULL 货件管理
version: 2.0.0
category: 产品运营
tags: [FULL, 货件, 发货, 库存, 预约, 箱唛, 标签]
difficulty: ⭐⭐⭐
requires:
  - ziniao-cli >= 1.0.0
  - mercadolibre-store-auth
  - feishu-config
triggers:
  - pattern: 货件|FULL|创建货件|发货|预约|箱唛|标签|仓库|Enviar productos
entry: scripts/fulfillment.js
output:
  format: markdown
  push_to: [any]
push:
  enabled: true
  type: card
  template: completion
---

# 美客多 FULL 货件管理

基于标准化 SOP（美客多货件创建标准化SOP.xlsx）的 8 步操作流程。

## 操作警告与触发生效条件

步骤 3/4/5/7/8 会修改卖家后台数据——每步执行前必须确认用户已批准。步骤 1/2/6 为只读操作，可直接执行。
触发条件：用户要求「创建货件」「发 FULL」「FULL 发货」；需要「下载标签」「打印箱唛」；或要「取消预约」。

## 执行流程

### 步骤 1：前期准备 [只读 — 直接执行]
导航到 FULL 管理页，检查：货件列表中是否有「Vencido」（过期）货件、「En preparación」货件数量、库存限制警告。
输出当前 FULL 状态摘要。禁止超库容创建，下架/违规 Listing 禁止创建货件。

### 步骤 2：进入货件创建入口 [只读 — 直接执行]
导航到 FULL 管理页，点击「Enviar productos」按钮。确认进入官方正品后台页面。

### 步骤 3：选择产品与数量 [⚠️ 写操作 — 需用户批准]
搜索 SKU → 输入数量（不超系统限制）→ 核对后「Continuar」→ 弹窗确认「Continuar con mi plan actual」。
用户需提供 SKU 列表 + 各 SKU 数量。失败处理：超时停止重复提交，等 2 分钟核查后台状态；无有效货件则核对 SKU/数量重新创建；产品异常修正后操作。

### 步骤 4：货件预约时间 [⚠️ 写操作 — 需用户批准]
选择配送方式「Vehículo particular」→ 选 30 天之后的时间 →「Continuar」。禁止修改仓库地址。

### 步骤 5：包装确认 [⚠️ 写操作 — 需用户批准]
勾选确认框 → 确认「Confirmo que el producto tiene un empaque correcto」→「Continuar」。超时禁止重复提交，先核查后台状态。

### 步骤 6：标签下载 [只读 — 直接执行]
勾选标签 → 选择「1 seleccionado de 1」→「Descargar etiquetas」→「Descargar」。
命名规则：`Código ML` + `SKU`（例：`YNWE47995+HW-MX-026-01`）。标签贴单品外包装，不可与箱唛混贴。

### 步骤 7：打印箱唛 [⚠️ 写操作 — 需用户批准]
选择「Bultos sin agrupar en pallets」→ 填写箱数 → 下载箱唛。
命名规则：`货件号` + `SKU`（例：`71667033+HW-MX-026-01`）。

### 步骤 8：取消预约时间 [⚠️ 写操作 — 需用户批准]
点击「Editar」→「Cancelar reserva」→ 确认「Cancelar cita」。预约仅为下载箱唛使用，下载完成后必须取消。

---

## 异常处理

| 异常问题 | 核心原因 | 解决方案 |
|----------|----------|----------|
| 货件创建超时/失败 | 网络延迟、超库容、产品异常 | 停止重复提交，等 2 分钟核查后台；无有效货件则核对 SKU/数量重新创建；产品异常修正 Listing |
| 货件拆分（大小件混装） | 常规件与大件同批创建 | 按系统生成的多个货件 ID 分开打包、单独贴标、分别入仓，禁止混装 |
| 标签无法下载/条码失效 | 货件状态异常、系统缓存 | 刷新页面重新下载，确保条码可正常扫描入库 |
| 步骤 3-5 超时 | 页面加载慢、网络波动 | 禁止重复提交；先核查后台状态确认是否已生成货件 |

## 作业规范与禁止事项

详见脚本 forbidden 命令输出的禁止事项清单。核心原则：严格按系统推荐库容创建，所有操作留存货件 ID/创建时间/发货时间。

## 操作摘要格式

完成全部步骤后输出操作摘要：

```json
{
  "shipment_id": "71667033",
  "created_at": "2026-07-19T15:30:00",
  "skus": ["HW-MX-026-01"],
  "total_units": 90,
  "appointment": "2026-08-19T09:00:00",
  "appointment_cancelled": true,
  "labels_downloaded": true,
  "box_labels_printed": true,
  "status": "completed",
  "warnings": []
}
```

## 复盘归档

每次完成货件操作后同步输出日报存档：货件 ID、创建时间、SKU 清单、数量、异常状态。详见 #7 店铺日报机制。
如需撤回货件请在 FULL 管理页操作「Cancelar envío」（仅限未发送状态），已发送货件联系平台客服并提供货件 ID。
每单操作日志包含完整操作记录截图，存档至对应店铺日报文档，便于追溯复盘。
