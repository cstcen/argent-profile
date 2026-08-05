# 已知陷阱与修复（踩坑清单）

按出现频率排序的经验教训。改动脚本或排障时先对照本清单，避免重复踩坑。

## 导航栏误触

- 现象：checkbox 选择器命中顶部导航栏的用户切换组件（`#nav-header-user-switch`），勾选错误元素
- 修复：checkbox 限定到 `main` 容器内查找；`_scope` 参数只对 checkbox 生效

## 货件号 ≠ URL 编号

- 现象：从 URL（inbound 编号）提取货件号，与实际货件号不一致
- 修复：货件号从 DOM 标题元素提取，不使用 URL inbound 编号

## React 动态 ID 大小写

- 现象：动态 ID 大小写不稳定（`_r_` vs `_R_`），选择器漏匹配
- 修复：CSS `[id^="_r_"]` 区分大小写，需同时覆盖两种写法；数量输入框不是 `type="number"`

## 产品标 PDF 前缀

- 现象：下载的产品标文件名带 `Envio-` 前缀，按 `*Etiquetas-de-productos.pdf` 匹配时漏掉
- 修复：文件名模式用 `Envio-*-Etiquetas-de-productos.pdf`

## 下载目录

- 下载目录为 `ziniaobrowserdatas/{店铺名}/`，多店铺时按店铺名区分，勿混用

## PDF 命名规则

- 产品标：`产品标 + SKU + ML码 + 品名 + 店名`
- 箱唛：`货件号 + 箱数箱 + 店名`

## 排除无关 PDF

- 排除 listado / Descargar / preparation 产品列表等非目标 PDF 文件

## 货件终态跳过

- 货件终态 `Reserva cancelada` 应跳过，不再继续处理

## wait_for_url 时序

- `wait_for_url` 在 hub-v2 之后提取，避免过早读取旧页面内容

## 页面切换等待

- 页面切换用旋转蒙层（spinner）等待替代固定 sleep，避免竞态与超时
