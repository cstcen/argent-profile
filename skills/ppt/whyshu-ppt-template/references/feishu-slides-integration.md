# WHYSHU PPT — 飞书 Slides 集成

飞书支持创建演示文稿，设计系统与 pptxgenjs 相同但 API 不同。**推荐用 Python 构建 JSON 数组，管道传入 `--slides`。**

## 前置条件

```bash
lark-cli auth login --domain slides  # 首次需授权
```

若 Hermes 终端 Keychain 被阻断，先在 Terminal.app 手动运行：
```bash
lark-cli config keychain-downgrade
```

## 创建方式

### 一步法（推荐，1-10 页简单内容）

```bash
lark-cli slides +create --as user \
  --title "标题" \
  --slides '["<slide xmlns=\"http://www.larkoffice.com/sml/2.0\">...</slide>",...]'
```

- `--slides` 必须是 JSON 字符串数组，每元素是完整 `<slide>`
- 内联 JSON 可达 42KB
- 支持 Python 构建 JSON 后管道传入

### 两步法（逐页精确控制）

1. `lark-cli slides +create --as user --title "标题"` → `xml_presentation_id`
2. `lark-cli slides xml_presentation.slide create` 逐页追加

## `<slide>` 创建关键发现

### xmlns 必须
每个 `<slide>` 必须带 `xmlns="http://www.larkoffice.com/sml/2.0"`。
缺少 xmlns → 3350001 invalid param（最常见失败原因）。

### Python json 写文件 vs 管道
- Python `json.dump` 写入文件再 `@file` 读取 → 偶尔 3350001（疑似文件 BOM 或编码问题）
- Python 构建 JSON 字符串，shell 管道传入 → 可靠 ✅
- Shell 内联 XML 字符串（变量 + jq 构造 payload）→ 可靠 ✅

### 3350001 完整排障
| 原因 | 检查 |
|------|------|
| 缺 xmlns | `<slide>` 是否包含 `xmlns="http://www.larkoffice.com/sml/2.0"` |
| 未转义字符 | `&`→`&amp;`，`<`→`&lt;`（注意 URL 参数中的 `&`） |
| 属性格式 | `bold="true"` 必须是字符串，不能写 `bold=true` |
| content 结构 | 文本必须在 `<content><p>...</p></content>`，不能裸写文案 |
| 嵌套引号冲突 | shell/json/XML 三层引号不可互相打断 |

## XML Schema 要点

- 根元素: `<presentation xmlns="http://www.larkoffice.com/sml/2.0" width="960" height="540">`
- 页面: `<slide><style><fill><fillColor color="rgb(13,27,42)"/></fill></style><data>...</data></slide>`
- 文本: `<shape type="text"><content textType="title" fontSize="48" color="rgb(255,255,255)"><p>文字</p></content></shape>`
- 卡片: `<shape type="rect"><fill><fillColor color="rgb(22,32,48)"/></fill></shape>`
- 线条: `<line startX="80" startY="200" endX="880" endY="200"><border color="rgb(30,42,58)" width="1"/></line>`
- 渐变必须 `rgba()` + 百分比停靠点，否则回退白色
- `textType`: title(54pt) / headline(38pt) / body(16pt) / caption(12pt) — 可按 shape 级 fontSize 覆写

## 圆角与图形

- 圆角矩形: `<shape type="rect" radius="...">` —— 用 `radius` 属性
- 圆形/椭圆: 无原生 OVAL，用 `radius="999"`（全圆角矩形≈圆形）
- 透明度: `opacity` 属性（0.0-1.0），备选方案用 `rgba()` 颜色

## 暗底对比度

在 `rgb(13,27,42)` 深海军蓝背景上：
| 文字色 | 对比度 | 可用性 |
|--------|--------|--------|
| rgb(255,255,255) | 15.3:1 | ✅ 正文 |
| rgb(200,210,220) | 7.1:1 | ✅ 辅助文字 |
| rgb(0,200,150) | 5.2:1 | ✅ 强调色 |
| rgb(136,153,170) | 3.0:1 | ❌ 不可读 |
| rgb(200,210,220) | 7.1:1 | ✅ 推荐 |

## 卡片图标可见性

emoji 在飞书 Slides 中默认颜色可能为 #000000，在暗底卡片（如 `rgb(47,31,10)`）上完全不可见。
- 替代方案：用数字圆圈代替 emoji（`<shape type="rect" radius="17">` + 数字文字）
- 或显式设置 emoji 的 `color` 属性

## WHYSHU 色值映射

| pptxgenjs | 飞书 XML |
|-----------|---------|
| `0D1B2A` | `rgb(13,27,42)` |
| `162030` | `rgb(22,32,48)` |
| `00C896` | `rgb(0,200,150)` |
| `FFFFFF` | `rgb(255,255,255)` |
| `8899AA` | `rgb(136,153,170)` — ❌ 太暗 |
| `F5A623` | `rgb(245,166,35)` |
| `0F2F1A` | `rgb(15,47,26)` |
| `2F1F0A` | `rgb(47,31,10)` — 用作卡片底时注意图标可见性 |
| `1A2A3A` | `rgb(26,42,58)` |

## 限制

- `--slides @file.json` 不支持 → 内联 JSON 或两步法
- `--slides` 仅接受 JSON 字符串数组
- 逐页 create 后再回读验证页数和内容

## 部署排障：新路由 404（2026-07-29 实测）

**现象**：在 argent.js 新增路由，代码已上传 PM2 重启后始终 404，其他路由正常。
**根因**：PM2 `ecosystem.config.cjs` 的 `script: "app.js"` 导入 `./routes/argent.js`，部署一直在改 `src/routes/argent.js`（死代码）。
**解决**：确认 PM2 实际加载的文件路径后再修改。