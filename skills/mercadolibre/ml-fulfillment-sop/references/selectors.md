# MercadoLibre FULL 页面 CSS 选择器映射

## 维护说明
- 美客多后台使用 **Andes** 设计系统（`andes-table`、`andes-button`、`andes-card` 等）
- 原生 `querySelectorAll` **不支持** `:contains()` / `:has-text()` 伪类——这些是 jQuery/Sizzle 扩展
- 数据提取策略：CSS 选择器定位结构容器 → JavaScript 遍历 textContent 匹配内容
- 每次 UI 变更后更新「最后验证日期」
- 关键页面：`https://myaccount.mercadolibre.com.mx/shipping/inbounds`

## 已验证信息（2026-07-27）

| 项目 | 值 |
|------|-----|
| 表格类名 | `andes-table` |
| 表格列（th） | Envío / Declaradas-Aptas / Cita reservada / Cargo aplicado / Estado / Acciones |
| 按钮组件 | `andes-button`（A 标签或 BUTTON 标签） |
| 状态文本容器 | `TD.andes-table__column` → `DIV.andes-typography` |
| 操作列容器 | `TD.andes-table__column` → `DIV.table-actions-column` |
| 卡片容器 | `andes-card` |
| 已验证货件状态值 | Vencido, En preparación, Pendiente de recepción, Procesamiento finalizado |

---

## 步骤 1：前期准备 [已验证 ✅]

### 选择器（CSS 结构定位）

| 元素 | CSS 选择器 | 验证状态 | 说明 |
|------|-----------|----------|------|
| 货件列表表格 | `table.andes-table` | ✅ 已验证 | 页面唯一表格 |
| 货件行 | `table.andes-table tbody tr` | ✅ 已验证 | 每行一个货件 |
| 状态列单元格 | `td.andes-table__column` (第 5 列, 0-indexed) | ✅ 已验证 | 表头第 5 列 = "Estado" |
| 操作列单元格 | `td.andes-table__column` (第 6 列) | ✅ 已验证 | 表头第 6 列 = "Acciones" |
| 库容警告容器 | `.andes-card`（含 "liberar espacio" 文本） | ✅ 已验证 | 出现在页面顶部 |

### 数据提取方式（JavaScript）

由于 Andes 组件 class 名不包含业务语义，数据提取**必须用 textContent 匹配**：

```javascript
// 检查过期货件
const rows = document.querySelectorAll('table.andes-table tbody tr');
const vencidos = Array.from(rows).filter(row => 
  row.textContent.includes('Vencido')
);

// 统计各状态数量
const statuses = {};
rows.forEach(row => {
  const cells = row.querySelectorAll('td');
  const status = cells[4]?.textContent?.trim(); // 第 5 列 = Estado
  if (status) statuses[status] = (statuses[status] || 0) + 1;
});

// 检测库容警告
const warning = Array.from(document.querySelectorAll('.andes-card'))
  .find(card => card.textContent.includes('liberar espacio'));
```

---

## 步骤 2：进入创建入口 [已验证 ✅]

| 元素 | 定位方式 | 验证状态 | 说明 |
|------|----------|----------|------|
| "Enviar productos" 按钮 | `a.andes-button` 且 textContent === "Enviar productos" | ✅ 已确认组件类型 | ⚠️ 库容满时此按钮不显示/禁用，页面显示"Para enviar productos, primero deberás liberar espacio..." |

```javascript
const enviarBtn = Array.from(document.querySelectorAll('a.andes-button'))
  .find(btn => btn.textContent.trim() === 'Enviar productos');
```

---

## 步骤 3-5：创建货件表单

### 步骤 3：选择产品与数量 [✅ 已部分验证]

> 已验证：配送计划页面的 SKU 搜索、数量填写、继续按钮。

| 元素 | 定位方式 | 验证状态 |
|------|----------|----------|
| SKU 搜索框 | `input[placeholder*="搜索产品"]` | ✅ 已验证 |
| 数量输入 | `input.andes-form-control__field`（搜索结果行的可见 input） | ✅ 已验证 |
| "继续" 按钮 | `button.andes-button` + textContent === "继续" | ✅ 已验证 |
| 明星产品弹窗 "维持现有方案" | textContent 匹配 → click 关闭 | ✅ 已验证 |

### 步骤 4：预约时间 [✅ 已验证]

> 已验证：配送方式下拉、focus-ui-calendar 日期选择、时间槽、确认按钮。
> 页面：`/shipping/inbounds/{id}/appointment-v2`

| 子步骤 | 元素 | 定位方式 | 验证状态 |
|--------|------|----------|----------|
| 配送方式下拉 | `[id*=shipment-type-selection]` | id 前缀匹配 | ✅ |
| "Vehículo particular" | 叶子 SPAN，textContent === "Vehículo particular" | textContent 精确匹配 | ✅ |
| 日期输入框 | `input[readonly][id^="_R_"]` | 只读，动态 ID 以 `_R_` 开头 | ✅ |
| 当前月份 | `.focus-ui-datepicker__content__month-year` | 如 "julio 2026" | ✅ |
| 下月按钮 | `.focus-ui-datepicker__content__next_month_selector` | aria-label="next month" | ✅ |
| 上月按钮 | `.focus-ui-datepicker__content__back_month_selector` | aria-label="back month" | ✅ |
| 可选日期 | `div.day:not(.day--disabled)` | 排除 `day--disabled` 的格子 | ✅ |
| 已选日期 | `div.day--selected` | 已选日期（可能同时 disabled） | ✅ |
| 时间槽 | `.date-time-card-content` 内 textContent 匹配 `/\d{1,2}:\d{2}/` 的叶子元素 | 动态渲染 | ✅ |
| 确认按钮 | `button:not([disabled])` + textContent === "Confirmar" | 需等时间槽选定后才启用 | ✅ |

**自动化算法（⚠️ 2026-07-27 更新：robustClick 模式）**：
```javascript
// robustClick: 攻克 Andes React 组件的鼠标事件序列
function rc(el){ el.scrollIntoView();el.focus();
  el.dispatchEvent(new MouseEvent("mousedown",{bubbles:true}));
  el.dispatchEvent(new MouseEvent("mouseup",{bubbles:true}));
  el.dispatchEvent(new MouseEvent("click",{bubbles:true,cancelable:true,view:window})); }

// 1. 选配送方式
rc(document.querySelector("[role=combobox]")); // 打开下拉
// 在 [role=option] 中找 textContent 含 "Vehículo particular" → rc()

// 2. 选日期
rc(document.getElementById("_r_j_")); // 打开日历
// 翻月: rc(document.querySelector("[aria-label=\"next month\"]"));
// ⚠️ 日历格子点击: rc() 无法触发 React onChange
// → **此步目前需人工点击**，其余 7 步全自动

// 3. 选时间
// 在 date-time-card-content 内找 textContent 匹配 /\d{1,2}:\d{2}/ → rc()

// 4. 确认
// 先点日历 Confirmar (top<700 的), 再点主 Confirmar (_r_k_)
```

### 步骤 5：包装确认 [⚠️ 待验证]

| 元素 | 定位方式 | 验证状态 |
|------|----------|----------|
| 包装确认复选框 | 待确认 | ⚠️ 待验证 |

---

## 步骤 6-8：标签/箱唛/取消

### 步骤 6：标签下载 [✅ 已验证]

> 已验证：通过 Hub 页「Revisar」链接进入标签页、复选框（Andes 组件需 MouseEvent）、下载弹窗、确认。

| 元素 | 定位方式 | 验证状态 | 备注 |
|------|----------|----------|------|
| Hub 页入口（初始） | `[data-testid="link-identifiers"]`（卡片本身 `role="button"`） | ✅ | 首次进入，右侧 SVG 箭头是卡片视觉元素，整个卡片可点击 |
| Hub 页入口（二次） | `a[href*="labeling"]` + textContent === "Revisar" | ✅ | 进入过一次后出现 |
| 产品展开按钮 | `button` + textContent === "Producto" | ✅ | 展开产品列表 |
| 复选框 | `input[type=checkbox]` 在 `.andes-checkbox__checkbox` 内 | ✅ | **必须用 MouseEvent 而非 .click()** |
| Descargar etiquetas | `button` + textContent === "Descargar etiquetas" + `!disabled` | ✅ | 复选框勾选后启用 |
| 弹窗 PDF 选项 | `button` + textContent === "PDF" + `inModal` | ✅ | 默认已选中 |
| 弹窗 Descargar | `button` + textContent === "Descargar" + `inModal` | ✅ | 触发下载 |
| Confirmar | `button` + textContent === "Confirmar" | ✅ | 完成标签步骤 |

**关键 Pitfall**：Andes 复选框是 React 受控组件，`cb.click()` **不会触发 React onChange**——必须用：
```javascript
cb.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, view: window}));
```
复选框 DOM `checked` 属性不会更新，但 React 内部状态会正确更新。

---

## 从页面内容提取数据的完整模式

`ziniao-cli page content --content-format text` 返回的纯文本已包含所有业务数据，正则提取比 CSS 选择器更可靠：

```javascript
// 从 page content 文本中提取货件列表
const text = pageContent; // ziniao-cli page content 返回的纯文本

// 匹配货件行：货件号 + 声明数/合格数 + 日期(可选) + 状态 + 操作
const shipmentPattern = /#(\d{8})\s+(\d+)\s*\/\s*(\S+)\s+([\d\/]+\s+\S+(?:\s+\d+:\d+)?\s*(?:hs)?\s+[^VEPC]*)?\s+(Vencido|En preparación|Pendiente de recepción|Procesamiento finalizado)\s+(Revisar detalle|Completar envío)/g;

let match;
while ((match = shipmentPattern.exec(text)) !== null) {
  // match[1] = 货件号, match[2] = 声明数, match[3] = 合格数
  // match[5] = 状态, match[6] = 操作
}
```

**优势**：纯文本提取不依赖 CSS 选择器，UI 改版后仍可工作（只要文本内容不变）。

---

## 注意
- `:has-text()` 和 `:contains()` **不可用**——这是 jQuery 扩展，标准 CSS / `querySelectorAll` 不支持
- Andes 组件 class 名（`andes-button__content`、`andes-typography`）是内部实现细节，可能随版本变更
- **推荐策略**：结构定位用 CSS（`table.andes-table`），内容匹配用 textContent
- 纯文本正则提取（`page content`）是最稳定的数据采集方式，优先使用
