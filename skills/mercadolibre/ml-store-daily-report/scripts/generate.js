#!/usr/bin/env node
/**
 * ml-store-daily-report generate script
 * Converts extract.js JSON output into a formatted Chinese Markdown daily report.
 *
 * Input (single store, backward compatible):
 *   { "store": "墨西哥站", "orders": {...}, "reputation": {...}, ... }
 *
 * Input (multi store, v1.1.0):
 *   { "stores": [{ "store": "墨西哥站", "orders": {...}, ... }, ...] }
 *
 * Usage:
 *   node generate.js data.json              # Markdown output
 *   node generate.js data.json --json       # JSON output {markdown, push}
 *   node generate.js data.json --push       # Push card JSON only
 *   cat data.json | node generate.js        # Markdown from stdin
 */

function normalizeInput(data) {
  if (data.stores && Array.isArray(data.stores) && data.stores.length > 0) {
    return data.stores;
  }
  // Single store — wrap in array for uniform processing
  return [data];
}

function generateReport(data) {
  const stores = normalizeInput(data);
  const lines = [];
  const date = stores[0]?.date || new Date().toISOString().slice(0, 10);

  // ---- Header ----
  if (stores.length === 1) {
    const s = stores[0];
    const storeName = s.store && s.store !== '未知站' ? s.store : '墨西哥站';
    lines.push('# 美客多运营日报');
    lines.push(`**日期**: ${date} | **店铺**: ${storeName} | **站点**: 墨西哥`);
  } else {
    lines.push('# 美客多运营日报（多店铺汇总）');
    lines.push(`**日期**: ${date} | **店铺数**: ${stores.length}`);
  }
  lines.push('');

  // ---- 汇总概览 ----
  if (stores.length > 1) {
    lines.push('## 📦 汇总概览');
    lines.push('| 店铺 | 订单量 | 销售额 | 退货 | 声誉 |');
    lines.push('|------|--------|--------|------|------|');

    let totalOrders = 0, totalAmount = 0, totalReturns = 0;
    for (const s of stores) {
      const count = s.orders?.count ?? 0;
      const amount = s.orders?.total_amount_mxn ?? 0;
      const returns = s.after_sales?.pending ?? 0;
      const repScore = s.reputation?.score || '—';
      totalOrders += Number(count) || 0;
      totalAmount += Number(amount) || 0;
      totalReturns += Number(returns) || 0;
      lines.push(`| ${s.store || '—'} | ${count || '—'} | ${amount ? '$' + amount.toFixed(2) : '—'} | ${returns || '—'} | ${repScore} |`);
    }
    lines.push(`| **合计** | **${totalOrders}** | **$${totalAmount.toFixed(2)}** | **${totalReturns}** | — |`);
    lines.push('');
  }

  // ---- 逐店详情 ----
  for (const s of stores) {
    const sectionTitle = stores.length > 1
      ? `## 📊 ${s.store || '未知站'}`
      : '## 📦 数据概览';

    lines.push(sectionTitle);
    if (stores.length === 1) {
      lines.push('| 指标 | 今日 | 环比昨日 |');
      lines.push('|------|------|----------|');
    } else {
      lines.push('| 指标 | 今日 |');
      lines.push('|------|------|');
    }

    const orderCount = s.orders?.count ?? '—';
    const totalAmount = s.orders?.total_amount_mxn != null
      ? `$${s.orders.total_amount_mxn.toFixed(2)}`
      : '—';
    const returns = s.after_sales?.pending ?? 0;

    lines.push(`| 订单量 | ${orderCount} |${stores.length === 1 ? ' — |' : ''}`);
    lines.push(`| 销售额 | ${totalAmount}${totalAmount === '—' ? '' : ' MXN'} |${stores.length === 1 ? ' — |' : ''}`);
    lines.push(`| 退货/售后 | ${returns} |${stores.length === 1 ? ' — |' : ''}`);
    // Show order detail count only for real parsed items (skip metrics synthetic entry)
    const realItems = (s.orders?.items || []).filter(item => item.sku !== 'METRICS');
    if (realItems.length > 0) lines.push(`| 订单明细数 | ${realItems.length} |${stores.length === 1 ? ' — |' : ''}`);
    lines.push('');

    // 发货
    if (s.shipping) {
      const sh = s.shipping;
      lines.push('### 🚚 发货');
      lines.push(`今日 ${sh.today ?? 0} | 未来 ${sh.upcoming ?? 0} | 运输中 ${sh.in_transit ?? 0}`);
      lines.push('');
    }

    // 声誉
    const rep = s.reputation || {};
    lines.push('### 🌟 声誉');
    if (rep.level) {
      const emoji = rep.level.includes('Gold') ? '🥇' : rep.level.includes('Platinum') ? '🥈' : rep.level.includes('Silver') ? '🥉' : '';
      lines.push(`**${emoji} ${rep.level}**${rep.score ? `  评分: ${rep.score}` : ''}`);
    } else if (rep.score) {
      lines.push(`**评分**: ${rep.score}`);
    }
    if (rep.complaints) {
      const c = rep.complaints;
      const parts = [];
      if (c.reclamos_pct != null) parts.push(`Reclamos ${c.reclamos_pct}`);
      if (c.mediaciones_pct != null) parts.push(`Mediaciones ${c.mediaciones_pct}`);
      if (c.cancel_pct != null) parts.push(`取消率 ${c.cancel_pct}`);
      if (c.envio_err_pct != null) parts.push(`错误发货 ${c.envio_err_pct}`);
      if (parts.length > 0) lines.push(parts.join(' | '));
    }
    if (rep.new_questions != null) lines.push(`新买家提问: ${rep.new_questions} 条`);
    const hasRep = rep.level || rep.score || rep.complaints || rep.new_questions != null;
    if (!hasRep) lines.push('*暂无声誉数据*');
    lines.push('');

    // FULL
    const full = s.full || {};
    lines.push('### 📦 FULL 库存');
    if (full.pending_count != null) lines.push(`待处理货件: ${full.pending_count} 件`);
    if (full.not_apt_total > 0) lines.push(`不适配 FULL: ${full.not_apt_total} 单位`);
    if (full.shipments && full.shipments.length > 0) {
      for (const sh of full.shipments) {
        const emoji = sh.status === 'Vencido' ? '❌' : sh.status === 'En preparación' ? '🔄' : '✅';
        lines.push(`- ${emoji} #${sh.id || '—'} ${sh.status} | 申报:${sh.declared ?? '?'} 适配:${sh.apt_for_full ?? '?'} | ${sh.appointment || '—'}`);
      }
    } else {
      lines.push('*暂无 FULL 货件数据*');
    }

    // Alerts
    const alerts = _collectAlerts(s);
    if (alerts.length > 0) {
      lines.push('');
      lines.push('### ⚠️ 告警');
      for (const a of alerts) lines.push(`- ${a}`);
    }

    lines.push('');
  }

  return lines.join('\n');
}

function _collectAlerts(data) {
  const items = [];
  const rep = data.reputation || {};
  const full = data.full || {};

  if (data.alerts && data.alerts.length > 0) {
    for (const a of data.alerts) {
      switch (a) {
        case 'auth_code_missing': items.push('🔴 授权码缺失'); break;
        case 'no_orders_found': items.push('🟡 无订单数据'); break;
        case 'pending_after_sales': items.push(`🔴 售后待处理 ${data.after_sales?.pending ?? 0} 单`); break;
        default: items.push(`🟡 ${a}`);
      }
    }
  }
  if (rep.complaints) {
    const c = rep.complaints;
    if (c.reclamos_pct && parseFloat(c.reclamos_pct) > 5) items.push(`🔴 Reclamos 过高 ${c.reclamos_pct}`);
    if (c.mediaciones_pct && parseFloat(c.mediaciones_pct) > 3) items.push(`🔴 Mediaciones 过高 ${c.mediaciones_pct}`);
    if (c.cancel_pct && parseFloat(c.cancel_pct) > 5) items.push(`🟡 取消率过高 ${c.cancel_pct}`);
  }
  if (full.shipments) {
    const expired = full.shipments.filter(s => s.status === 'Vencido');
    if (expired.length > 0) items.push(`🔴 ${expired.length} 件货件过期`);
  }
  return items;
}

// ================================================================
// Push data builder
// ================================================================

function buildPushData(data) {
  const stores = normalizeInput(data);
  const multi = stores.length > 1;
  const date = stores[0]?.date || new Date().toISOString().slice(0, 10);

  // Aggregate metrics
  let totalOrders = 0, totalAmount = 0, totalReturns = 0;
  const perStore = [];
  const allAlerts = [];

  for (const s of stores) {
    const count = Number(s.orders?.count) || 0;
    const amount = Number(s.orders?.total_amount_mxn) || 0;
    const returns = Number(s.after_sales?.pending) || 0;
    totalOrders += count;
    totalAmount += amount;
    totalReturns += returns;

    perStore.push({ store: s.store || '未知', count, amount, returns, score: s.reputation?.score || '' });

    const alerts = _collectAlerts(s);
    if (alerts.length > 0) allAlerts.push({ store: s.store, alerts });
  }

  const metrics = [
    { label: '总订单', value: String(totalOrders), change: '' },
    { label: '总销售额', value: `$${totalAmount.toFixed(2)} MXN`, change: '' },
  ];
  if (totalReturns > 0) metrics.push({ label: '退货/售后', value: String(totalReturns), change: '' });

  // Per-store breakdown as highlight
  const breakdown = multi
    ? perStore.map(p => `${p.store}: ${p.count}单 $${p.amount.toFixed(2)}`).join('\n')
    : '';

  // Alerts
  let highlight = '';
  if (allAlerts.length > 0) {
    const alertSummary = allAlerts.map(a => `⚠️ ${a.store}: ${a.alerts.slice(0, 2).join('、')}`).join('\n');
    highlight = alertSummary;
  } else if (multi) {
    highlight = '今日所有店铺运营数据正常';
  } else {
    highlight = '今日运营数据正常';
  }

  return {
    card: {
      template: 'daily_report',
      data: {
        title: multi ? `美客多运营日报（${stores.length}店汇总）` : '美客多运营日报',
        date,
        store: multi ? `${stores.length} 个店铺` : (stores[0]?.store || '—'),
        metrics,
        highlight: breakdown ? `${breakdown}\n\n${highlight}` : highlight,
      },
    },
  };
}

// ---- CLI Entry ----
function main() {
  const args = process.argv.slice(2).filter(a => a !== '--json' && a !== '--push');
  const mode = process.argv.includes('--push') ? 'push'
    : process.argv.includes('--json') ? 'json'
    : 'markdown';
  const fileArg = args[0];

  function processJSON(input) {
    try {
      const data = JSON.parse(input);
      const markdown = generateReport(data);

      if (mode === 'push') {
        console.log(JSON.stringify(buildPushData(data), null, 2));
      } else if (mode === 'json') {
        console.log(JSON.stringify({ markdown, push: buildPushData(data) }, null, 2));
      } else {
        console.log(markdown);
      }
    } catch (e) {
      console.error('Error: Invalid JSON input — ' + e.message);
      process.exit(1);
    }
  }

  if (fileArg) {
    const fs = require('fs');
    let content;
    try {
      content = fs.readFileSync(fileArg, 'utf8');
    } catch (e) {
      console.error(`Error: Cannot read file "${fileArg}" — ${e.message}`);
      process.exit(1);
    }
    processJSON(content);
  } else {
    let input = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { input += chunk; });
    process.stdin.on('end', () => {
      if (!input.trim()) {
        console.error('Error: No input received.');
        process.exit(1);
      }
      processJSON(input);
    });
  }
}

if (require.main === module) {
  main();
} else {
  module.exports = { generateReport, buildPushData, normalizeInput };
}
