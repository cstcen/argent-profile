---
id: ml-account-health
name: 美客多账户健康诊断
version: 1.0.0
category: 账号管理
tags: [账户健康, 违规, 限制, 政策合规, Reputación, 诊断]
difficulty: ⭐⭐
requires:
  - ziniao-cli >= 1.0.0
  - mercadolibre-store-auth
triggers:
  - pattern: 账户健康|违规|被封|限制|警告|政策|账号安全|诊断
entry: scripts/diagnose.js
output:
  format: markdown
  push_to: [any]
---

# 美客多账户健康诊断

## 目标
检查美客多卖家账户的整体健康状况——Reputación 评分、平台合规状态、账号限制/警告、Mediación 胜诉率——输出账户健康评分卡及风险提示。

## 触发生效条件
- 用户询问「店铺有没有问题」「账户健康」
- 用户收到平台通知/警告
- 用户想了解账户综合评级
- 定期检查（建议每周一次）

## 执行流程

### 步骤 1：获取 Reputación 评分
```bash
ziniao-cli page visit --store-id <ID> --url "<Reputación URL>" --wait-until networkidle
ziniao-cli page extract --store-id <ID> --mode page --format json
```
提取：
- Reputación 颜色等级（Rojo/Naranja/Amarillo/Verde/Azul）
- 综合评分（1-5 分）
- 各维度评分：发货速度、客服响应、商品描述一致性
- 近 90 天评价统计

### 步骤 2：检查平台合规状态
访问账户健康/合规页面：
- Listing 违规通知（Imágenes prohibidas、Título incorrecto 等）
- 类目限制（需批准的受限类目）
- 禁售品检测提醒
- 税务/财务合规状态

### 步骤 3：检测账号限制/警告
- 近期是否收到平台警告
- 是否有功能限制（如限制发帖、限制广告投放）
- 限制到期日期
- 解限条件

### 步骤 4：Mediación 胜诉率统计
- 历史纠纷总数
- 卖家胜诉数
- 买家胜诉数
- 进行中纠纷数
- 胜诉率趋势

### 步骤 5：输出健康评分卡
```markdown
# 账户健康评分卡
店铺: XX站 | 检查时间: YYYY-MM-DD HH:mm

## 🏆 Reputación
评分等级: Verde (4.5/5.0)

## ✅ 合规状态
状态: 正常
近期警告: 无

## ⚠️ 待处理项
| 项 | 严重程度 | 建议 |
|---|----------|------|
| — | — | — |

## 📈 综合评分: 92/100
评级: 优
```
综合评分算法：Reputación（40%）+ 合规（30%）+ 争议（30%）。

## 失败处理
| 错误 | 处理 |
|------|------|
| 合规页面无数据 | 检查店铺环境登录状态 |
| 部分指标页面不可访问 | 跳过该模块，在评分卡中标注「数据不可用」 |
| Reputación 页面结构变化 | 回退到基础提取方式 |

## 注意事项
- 账户健康检查结果仅反映抓取时刻的快照
- 严重警告（如销售权限限制）不会由本技能处理，需引导用户联系美客多客服
- 建议每周自动执行一次，异常时立即通知用户
