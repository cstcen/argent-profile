# 飞书 Slides 创建与部署排障

## 创建 PPT

### 一步法（推荐——10 页以内用此）
```bash
# Python 生成 JSON 数组写入文件
python3 -c "import json;json.dump(slides,open('/tmp/slides.json','w'),ensure_ascii=False)"
# 传入 CLI——必须内联，不能用 @file（lark-cli v1.0.53 不支持）
SLIDES_JSON=$(cat /tmp/slides.json)
lark-cli slides +create --as user --title "标题" --slides "$SLIDES_JSON"
```
42KB JSON 数组一次创建 10 页成功。比逐页 `slide.create` 可靠。

### 授权流程
```bash
# 首次使用 slides 领域需要单独授权
lark-cli auth login --domain slides
# → 扫码完成授权
lark-cli slides +create --as user --title "..."
```

### lark-cli 版本限制
- v1.0.53：**不支持 `--slides @file.json` 语法**，必须内联传 JSON
- v1.0.79+：可能支持 @file，但未验证
- `xml_presentation.slide.create` 逐页追加：每页 `--data` 必须用 `jq -n --arg` 构造，不能用 Python 文件

### 两步法（逐页追加）
```bash
lark-cli slides +create --as user --title "标题"  # 拿到 xml_presentation_id
lark-cli slides xml_presentation.slide create --as user \
  --params '{"xml_presentation_id":"XXX"}' \
  --data "$(jq -n --arg c '<slide xmlns="...">...</slide>' '{slide:{content:$c}}')"
```

## 关键错误与修复

### 3350001 invalid param
**根因 1**：`<slide>` 缺失 `xmlns="http://www.larkoffice.com/sml/2.0"`。
**根因 2**：用 Python `json.dump` 写文件后再 `lark-cli ... --data @file.json` 失败率高（v1.0.53 的 @file 语法不稳定）。改为 shell 变量直传 `--data "$PAYLOAD"` 或 `--slides "$JSON_ARRAY"`。
XML 中未转义字符（中文 ·、→ 可用，但 `&` 必须 `&amp;`）。

### 404 route not found（服务器端 — 飞书无关，但同类陷阱）
**经典陷阱**：PM2 ecosystem.config.cjs 中 `script: "app.js"` 从 `/home/whyshu/server/` 加载，not `src/app.js`。
`import argentRoutes from './routes/argent.js'` → `/home/whyshu/server/routes/argent.js`（not `src/routes/argent.js`）。
**排查**：`grep script ecosystem.config.cjs` → `ls app.js` 确认入口 → 跟踪 imports 路径。

### PM2 重启不生效
`pm2 restart` 可能不重载 ESM 模块。**可靠方式**：
```bash
pm2 delete <app> && cd /home/whyshu/server && pm2 start ecosystem.config.cjs
```
重启后 curl health 端点验证 `uptime`（秒数应该是几秒而非几百秒）。如果 `uptime` 是几十秒以上，说明旧进程仍在运行。

### install-cn.sh 里 $ARGENT_VERSION 不展开
echo 中 `$ARGENT_VERSION` 必须直写，不能从 Python 字符串模板生成——Python 的 `\$` 会被双重转义导致变量为空。
修复：用 `sed -i '101s/Argent /Argent \$ARGENT_VERSION /'` 直接改 shell 脚本行。

### slide 页面内容超出
飞书 Slides 画布 960×540。底部元素 y+h > 540 会被截断。设计时确保最后一行 `y + h ≤ 540`。

### emoji 在深色背景上不可见
飞书 Slides 中 emoji 文字默认继承父元素颜色或系统默认（常为 #000000），在 `rgb(13,27,42)` 背景上几乎不可见。用数字圆圈代替 emoji，或显式设置颜色为 `rgb(255,255,255)`。

### shell heredoc 中 Python 嵌套引号陷阱
在 `ssh ... << 'PYEOF'` 的 Python 字符串里嵌套 `'\"$INSTALL_DIR/VERSION\"'` 会导致反斜杠被 shell 提前解析。
**解决**：用 Python 直接写在本地文件然后 scp，或用 `sudo python3 -c "..."` 单行。

## 颜色映射

| .pptx | 飞书 XML | 说明 |
|-------|---------|------|
| `0D1B2A` | `rgb(13,27,42)` | bg |
| `00C896` | `rgb(0,200,150)` | teal |
| `FFFFFF` | `rgb(255,255,255)` | text |
| `200,210,220` | `rgb(200,210,220)` | body on dark bg（提亮后） |
| `8899AA` | ❌ 禁止用于暗底正文 | 对比度不足 |
