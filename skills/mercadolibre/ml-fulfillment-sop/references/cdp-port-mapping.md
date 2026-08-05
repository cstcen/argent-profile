# 多店铺 CDP 端口策略

紫鸟（ziniaobro）浏览器多店铺自动化时的端口分配、发现与缓存机制。

## 架构

- 紫鸟单进程单页面：同一时间只能打开一个店铺页面
- 多店铺通过 profile 切换实现，每店一个独立 profile

## 端口发现

- 每店独立 CDP 端口，**不硬编码**
- 从 ziniaobro 进程动态发现：用 `lsof -iTCP -sTCP:LISTEN -a -p <ziniaobro_pid>` 列出监听端口，再按端口范围探测确认
- 发现结果缓存到 `~/.hermes/scripts/cdp-port-map.json`（店铺名 → 端口映射）

## 窗口策略

- 去掉 `--headless`，每店独立可见窗口，便于人工核查与排查问题

## store 操作规范

- `store close` 必须用 `--id` 不用 `--name`（name 可能重复或变更）
- 禁止 `store close` 触发 ML 安全检测：关闭动作要干净利落，避免被平台风控（频繁切换/异常关闭会触发）
