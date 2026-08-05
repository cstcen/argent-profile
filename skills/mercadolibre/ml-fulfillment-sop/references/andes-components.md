# Andes React 组件交互规则

MercadoLibre 后台基于 Andes 设计系统（React）。原生 `.click()` 对多数 Andes 受控组件无效，必须模拟完整鼠标事件序列。

## 核心原则

- **PointerEvent / MouseEvent 全序列是触发 React onChange 的唯一可靠方式**
- React 受控组件监听合成事件（synthetic events），直接赋值 `.value` 或调用 `.click()` 不会更新内部状态
- 事件必须 `bubbles: true`，部分场景还需 `cancelable: true, view: window`

## 各组件可靠交互模式

### Checkbox

```javascript
cb.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
```

- DOM `checked` 属性不会更新，但 React 内部状态会正确切换
- 勾选是否生效以 UI 变化（如按钮启用）为准，不要读 DOM checked

### Number input（数量输入）

```javascript
el.click();
el.focus();
const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
setter.call(el, '90');
el.dispatchEvent(new Event('input', { bubbles: true }));
```

- 直接 `el.value = x` 不触发 React onChange，必须用 nativeSetter + 手动派发 input 事件
- 注意：数量输入框不是 `type="number"`，定位用 `input[id^="_r_"], input[id^="_R_"]`

### robustClick（通用按钮/元素）

```javascript
function rc(el) {
  el.scrollIntoView();
  el.focus();
  el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
}
```

- 用于：配送方式下拉、时间槽、翻月按钮、Confirmar 等
- 日历格子点击（`div.day`）rc() 可能仍无效，需人工介入或特殊处理

## 日历交互

### 灰圈定位 30 天后

- 用 `div.day--current` 定位今天，再数 31 格定位 30 天后
- 避免依赖绝对日期文本（月份翻页后失效）

### 双月视图同日区分

- 日历双月视图可能同时渲染两个相同日期，用 `parentRow.rowIndex` 区分所在行
- 先确定目标行，再在该行内点击目标日期

### next_month 按钮

- `next_month` 普通 `.click()` 无效，必须用 robustClick（mousedown + mouseup + click 全序列）

## 验证要点

- 每次改动交互代码后，用真实页面验证 React 状态是否更新（按钮启用/文本变化），不能只看 DOM 属性
- UI 改版后优先复查本文件与 `selectors.md` 的对应关系
