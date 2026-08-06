# 紫鸟多窗口并发与互斥锁设计（2026-08-06 实测）

> 背景：多个 SKILL（#10 FULL 货件、#7 日报等）都依赖紫鸟浏览器操作店铺。若 #10 长期占用窗口，其他 SKILL 可能冲突。本文记录实测结论与锁设计方案。

## 冲突本质

- 紫鸟 = 一个店铺一个窗口（一个 CDP 端口），同一窗口同一时刻只能有一个控制方有效驱动
- #10 FULL：Playwright `connect_over_cdp` 长流程（3-10 分钟，写操作多）
- #7 日报：`ziniao-cli page visit/content` 秒级只读
- 同一店铺窗口并发 → 导航互相打断、页面状态被踩踏

## 实测结论（2026-08-06，3 窗口在线：50566/50788/50876）

### 测试 A：双窗口 CDP 并发
两个 Playwright 客户端同时 `connect_over_cdp` 连接不同店铺端口，各自导航独立页面：

| 观察项 | 结果 |
|---|---|
| 同时连接 | ✅ 成功（0.2s） |
| 各自导航（A→resumen，B→inbounds）| ✅ A 导航后 B 页面保持原状 |
| 标题独立读取 | ✅ 互不影响 |

**结论：不同店铺窗口 = 完全隔离，可并行操作。**

### 测试 B：ziniao-cli vs CDP 跨窗口
`ziniao-cli page visit` 操作 1店 窗口期间，2店 窗口的 CDP 会话全程观察：

| 观察项 | 结果 |
|---|---|
| 2店 CDP URL 是否变化 | ✅ 纹丝不动（保持 resumen） |
| ziniao-cli 导航后 CDP 连 1店 窗口 | ✅ 可见导航结果（URL=inbounds） |
| 1店 CDP 继续接管导航 | ✅ 正常（可再导航到 resumen） |

**结论：**
1. ziniao-cli 操作 A 窗口不影响 B 窗口的 CDP 会话（跨窗口零干扰）
2. ziniao-cli 与 Playwright CDP 操作**同一窗口时共享同一浏览器页面状态**（互相可见导航）
3. 一个工具操作完释放后，另一个工具可正常接管（无残留锁定）

## 锁设计（flock 店铺粒度互斥锁）

```bash
# 锁文件：每店铺一把（storeId 见 STORE_MAP）
#   /tmp/ziniao-<storeId>.lock
# 拿锁（阻塞等待，适合 Agent 交互场景）：
flock /tmp/ziniao-27477945046190.lock -c "python3 orchestrator.py --mode full --allow-write"
# 拿锁（非阻塞，适合 cron 场景——拿不到跳过等下一轮）：
flock -n /tmp/ziniao-27477945046190.lock -c "..." || echo "窗口被占用，跳过本轮"
```

### 设计要点

| 项 | 决策 | 原因 |
|---|---|---|
| 锁粒度 | **按店铺**（3 店 3 把锁）| 跨窗口实测隔离，不同店铺可并行 |
| 覆盖工具 | **ziniao-cli + Playwright CDP 都要拿锁** | 同窗口共享页面状态，两种工具都改它 |
| 等待策略 | cron 场景 `-n` 非阻塞跳过；交互场景阻塞等待 | cron 5 分钟一轮，跳过等下轮即可 |
| 释放 | flock 进程退出自动释放 | 无需手动 unlock，异常退出也不死锁 |

### 需加锁的位置

- #10 FULL：`fulfillment_orchestrator.py`（Playwright CDP）
- #7 日报：extract 步骤（ziniao-cli page visit/content）
- 未来所有依赖紫鸟浏览器的 SKILL（#14 等）：统一在脚本入口拿锁

### 预期效果

- FULL 占用 1店 10 分钟时，2店/3店 日报完全不受影响（并行）
- 同一店铺任务天然串行，不踩踏
- 锁失败（窗口占用）→ cron 下一轮自动重试，无需人工介入
