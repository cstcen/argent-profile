# FULL 货件操作报告

**操作时间**: {{created_at}}
**货件 ID**: {{shipment_id}}
**店铺**: {{store}}
**状态**: {{status}}

---

## 货件摘要

| SKU | 数量 | 状态 |
|-----|------|------|
{{#each skus}}
| {{sku}} | {{qty}} | {{status}} |
{{/each}}

**总件数**: {{total_units}}

---

## 操作记录

| 步骤 | 名称 | 状态 | 备注 |
|------|------|------|------|
| 1 | 前期准备 | {{step1}} | |
| 2 | 进入创建入口 | {{step2}} | |
| 3 | 选择产品与数量 | {{step3}} | |
| 4 | 预约时间 | {{step4}} | {{appointment}} |
| 5 | 包装确认 | {{step5}} | |
| 6 | 标签下载 | {{step6}} | |
| 7 | 打印箱唛 | {{step7}} | |
| 8 | 取消预约 | {{step8}} | 预约已取消 |

---

## 警告

{{#if warnings}}
{{#each warnings}}
- ⚠️ {{this}}
{{/each}}
{{else}}
✅ 无异常
{{/if}}

---

## 下次建议

{{next_action}}

---

*本报告由 Argent 美客多 FULL 货件管理 Skill 自动生成*
