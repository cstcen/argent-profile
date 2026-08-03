---
name: whyshu-ppt-template
description: "WHYSHU/Argent 品牌 PPT 模版——深海军蓝+青绿配色，圆形编号，状态徽章，卡片式布局。pptxgenjs 生成，也支持飞书 Slides。"
---

# WHYSHU PPT 模版

生成 WHYSHU/Argent 品牌演示文稿。严格 16:9（10x5.625in），0.8in 四边距。也支持飞书 Slides（见 references/feishu-slides-integration.md）。

## 配色

| 变量 | 色值 | 用途 |
|------|------|------|
| bg | 0D1B2A | 背景 + 徽章文字色 |
| card | 162030 | 卡片底色 |
| teal | 00C896 | 主色——编号圆/运营/AI |
| amber | F5A623 | 警告/人工 |
| blue | 3B82F6 | 系统/信息 |
| text/sub | FFFFFF / 8899AA | 正文/次要 |
| succBg/failBg/missBg/infoBg | 0F2F1A/2F0A0A/2F1F0A/1A2A3A | 状态卡底色 |

## 排版

| 元素 | 字号 | 字重 | 颜色 |
|------|------|------|------|
| 标题编号 | 36pt | Arial Black | teal, w>=1.2in（防竖排） |
| 页面标题 | 26pt | Arial Bold | text |
| 卡片标题 | 17pt | Arial Bold | text |
| 正文 | 14pt | Arial Regular | sub |
| 英文/小字 | 10-11pt | Arial Regular | teal/sub |

## 组件

```javascript
// 徽章——深色文字 on 彩色背景（对比度 7.58:1 vs 白色 2.16:1）
function bd(s,x,y,w,h,t,c){...color:C.bg...}
// 圆形编号——深色数字 in teal 圆
function ci(s,x,y,d,n){...color:C.bg...}
// 标题编号——左上角 2 位数字
function tn(s,n,txt){...w:1.2...x:1.4...}
```

## 布局铁律

- 列位置精确计算，无重叠：每列 x+w 小于等于下列 x（留 0.2in 间隙）
- 同行元素同 y，填满行高：徽章/编号/文字共用 y 和 h，靠 valign:middle 居中
- 所有文字 margin:0：消除 pptxgenjs 默认 padding——这是最常见的对齐问题根因
- 卡片背景先于文字：代码顺序即 z-order
- 徽章文字深色 C.bg：对比度 7.58:1 vs 白色 2.16:1
- 编号圆文字深色 C.bg：22pt 大字号也需可读
- emoji 显式设 color:C.text：默认继承 #000000 在暗底不可见

## 防溢出

可用高度 = 5.625 - 0.8(bottom) - 1.8(start) = 3.025in

| 页面类型 | 计算 | 溢出对策 |
|----------|------|---------|
| 8行流水线 | 8x0.58+7x0.04=4.92in | 分2页(4+4行) |
| 10行表格 | 10x0.52+9x0.03=5.47in | 分2页(5+5行) |
| 3层架构 | 3x0.9+2x0.15=3.0in | 单行布局OK |
| 2x2保障 | 2x1.4+0.3=3.1in | CH<=1.35 |

## 幻灯片结构（10页）

| # | 标题 | 布局 |
|---|------|------|
| 1 | Hero | 大标题 + 装饰圆 |
| 2 | 目录 | 7行，RH=0.55in，线在 y+0.30 |
| 3 | 01 使用流程 | 3列卡片 |
| 4 | 02 流水线 1-4 | 4行，编号1-4 |
| 5 | 03 流水线 5-8 | 4行，编号5-8 |
| 6 | 04 表格-运营 | 5行，无顶部徽章 |
| 7 | 05 表格-系统 | 5行 |
| 8 | 06 可靠执行 | 2x2卡，CH=1.4 |
| 9 | 07 技术架构 | 单行，CH=0.9 |
| 10 | 结尾 | 呼应首页 |

## 表格列布局（防止重叠）

```
Col 1 (badge): 0.8-1.65  (w=0.85)
Col 2 (name):  1.9-3.3   (w=1.4)
Col 3 (type):  3.5-4.4   (w=0.9)
Col 4 (desc):  4.6-8.2   (w=3.6)
```

## 架构列布局

```
Col 1 (badge): 0.8-1.7  (w=0.9)
Col 2 (en):    1.9-3.3  (w=1.4)
Col 3 (name):  3.5-5.7  (w=2.2)
Col 4 (desc):  5.9-8.2  (w=2.3)
```

## 排版陷阱（多轮 QA 实战发现）

### 1. margin:0 是必需品
pptxgenjs 文本框默认有内边距。**所有列表行元素必须加 `margin:0`**，否则字面偏移 0.05-0.10"，同行元素纵向不对齐。
```js
s.addText(text,{x,y,w,h,valign:"middle",margin:0});
```

### 2. 同行元素 y 完全一致
列表中徽章、编号圆、标题、描述——**绝对禁止 `y+0.06` 等微小偏移**。所有同行元素共享 y 和 h，靠 `valign:"middle"` 居中。

### 3. 阴影对象禁止复用
pptxgenjs 内部会修改传入对象，必须用工厂函数每次新建：
```js
const shadow=()=>({type:"outer",color:"000000",blur:6,offset:2,opacity:0.18});
```

### 4. 1/2/3 列重叠问题
中文 4 字在 13pt 下约需 1.4" 宽度。列定位必须验证 `x1+w1 < x2`（至少 0.2" 间隙）。
表格列已验证公式：badge 0.8-1.65 → name 1.9-3.3 → type 3.5-4.4 → desc 4.6-8.2。

### 5. 可用高度 = 3.025"
16:9（10×5.625"）下，SY=1.8"，底边距 0.8" → 可用 5.625-0.8-1.8 = 3.025"。所有页面总行高必须 <=3.025"。

## 常见问题

- 目录分隔线与文字重叠 → 线在 y+0.30（紧贴文字底）
- 标题编号竖排 → w>=1.2in（两位数横排）
- 表格列重叠 → 验证每列 x1+w1 <= x2
- 徽章对比度低 → 用 C.bg 深色替代白色
- emoji 不可见 → 显式设 color:C.text（默认继承 #000000 在暗底上不可见）
- 行高不足文字溢出 → 中文 13pt 每字约 0.18in，1.4in 宽最多 4 字
- LAYOUT_WIDE 溢出到 16:9 → 检查页面尺寸，WIDE=13.3x7.5, 16:9=10x5.625

## 生成后 QA 流程

1. unzip -o ppt.pptx -d /tmp/qa/
2. 运行 pptx-layout-analysis 脚本检查重叠/对比度/溢出
3. 手工验证列位置：提取每列 x/w 确保无重叠
4. 固定检查清单：同行同 y、margin:0、列间距>=0.2in、总行高<=可用高度

## 参考

- references/feishu-slides-deployment.md — 飞书 Slides 部署排障（3350001 错误、PM2 路由、颜色映射）
- references/feishu-slides-integration.md — 飞书 Slides 创建流程
- templates/partner-ppt-build.js — pptxgenjs 生成脚本