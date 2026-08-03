---
id: ml-reputation-monitor
name: 美客多声誉监控
version: 1.0.0
category: 产品运营
tags: [声誉, Reputación, Opiniones, 差评告警, 评价管理, Preguntas]
difficulty: ⭐⭐⭐
requires:
  - ziniao-cli >= 1.0.0
  - mercadolibre-store-auth
triggers:
  - pattern: 差评|新评价|声誉|reputación|opiniones|preguntas|评分|投诉
entry: scripts/monitor.js
output:
  format: markdown
  push_to: [any]
---

# 美客多声誉监控

## 目标
跨店铺/跨站点聚合监控美客多 Reputación 评分、Opiniones（评价）和 Preguntas（买家提问），实现差评实时告警、未回复问题提醒和定期声誉健康报告。

## 触发生效条件
- 用户询问「有没有新差评」「检查声誉」
- 用户要求生成「本周声誉健康报告」
- 用户需要回复买家提问/差评时
- 定时任务告警触发

## 执行流程

### 步骤 1：获取店铺列表
```bash
ziniao-cli store list --format json
```

### 步骤 2：逐店检查 Reputación
对于每个活跃店铺：
```bash
ziniao-cli page visit --store-id <ID> --url "<Reputación URL>" --wait-until networkidle
ziniao-cli page extract --store-id <ID> --mode page --format json
```
提取：
- 当前 Reputación 颜色等级（Rojo/Naranja/Amarillo/Verde/Azul）
- 各分项评分（发货速度、客服响应、商品描述一致性）
- 近 30 天好评/差评/中评计数与变化趋势
- 低分主要原因关键词

### 步骤 3：检查新 Opiniones
提取未回复的评价列表，分类：
- **差评（Negativas）**：立即告警，附带 Order ID 和差评内容
- **中评（Neutrales）**：标记需关注
- **好评（Positivas）**：统计数量

### 步骤 4：检查 Preguntas 待回复
提取未回复的买家提问，评估紧急程度：
- 已超过 24 小时未回复 → 紧急
- 与产品质量/配送相关 → 优先

### 步骤 5：输出告警/报告

**告警模式**（有差评/超时回复时）：
```markdown
🚨 声誉告警 — 墨西哥站
店铺: Mi Tienda MX
新增差评: 1 条
内容: "Producto llegó dañado"
订单号: 123456789
建议: 48 小时内联系买家协商解决方案
```

**报告模式**（用户要求周报时）：
- 本周 Reputación 趋势
- 各店铺评分对比
- 高频差评关键词统计
- 回复时效统计
- 改进建议

## 失败处理
| 错误 | 处理 |
|------|------|
| Reputación 页面加载失败 | 检查店铺环境是否需重新登录 |
| 无新数据 | 输出「无变化」 |
| Preguntas 无法加载 | 跳过该模块，在报告中标注 |

## 注意事项
- 差评回复有 48 小时黄金窗口期，超时对 Reputación 影响增大
- 差评回复后买家可修改评价，回复措辞需专业礼貌
- 本技能不自动回复，仅输出回复建议由用户确认后执行
