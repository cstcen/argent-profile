#!/usr/bin/env node
/**
 * ml-store-daily-report extract script
 * Extracts sales summary, reputation, and FULL shipment data
 * from MercadoLibre seller pages.
 *
 * Designed to run as: ziniao-cli page exec --store-id <ID> --script extract.js
 * OR process the text output from: ziniao-cli page content --store-id <ID>
 *
 * 前置检查：飞书 CLI 配置
 *   推送日报前请先运行 argent feishu-setup
 *
 * Pages:
 *   - Sales:  https://www.mercadolibre.com.mx/ventas/omni/listado
 *   - Reput.: https://www.mercadolibre.com.mx/reputacion?from=seller_menu
 *   - FULL:   https://myaccount.mercadolibre.com.mx/shipping/inbounds
 */

const { execSync } = require("child_process");

/** 检查飞书 CLI 是否已配置 */
function checkFeishu() {
  try {
    execSync("which lark-cli", { stdio: "ignore" });
  } catch {
    console.error("❌ 未检测到 lark-cli。飞书推送不可用。");
    console.error("   请先安装 lark-cli 并运行 argent feishu-setup");
    process.exit(1);
  }
  try {
    const out = execSync("lark-cli auth status 2>&1", { encoding: "utf8", timeout: 5000 });
    if (!out.includes("OK") && !out.includes("ok") && !out.includes("授权")) {
      console.error("❌ 飞书未授权。请运行 argent feishu-setup 配置飞书 Bot");
      process.exit(1);
    }
  } catch {
    console.error("⚠️  飞书状态检查失败——如果未使用推送功能，可忽略。");
  }
}

// 使用 --push / --feishu 参数时进行检查
const args = process.argv.slice(2);
if (args.includes("--push") || args.includes("--feishu")) checkFeishu();

/**
 * Browser mode: runs via page exec, extracts from DOM
 */
function extractFromDOM() {
  const bodyText = document.body.innerText;
  return mergeAll(bodyText);
}

/**
 * CLI mode: process plain text from page content output
 */
function extractFromText(pageContent) {
  return mergeAll(pageContent);
}

/**
 * Merge sales + reputation + FULL into one result
 */
function mergeAll(text) {
  // Detect if this is the Metrics page (has aggregated KPIs) or order list
  const metricsPage = /绩效概览|总销售额/.test(text);
  const sales = metricsPage ? parseMetricsPage(text) : parseSalesData(text);
  const reputation = parseReputationData(text);
  const full = parseFullData(text);
  // 站点本地日期（toLocaleDateString 使用浏览器时区 = 店铺时区）
  const localDate = new Date().toLocaleDateString('sv-SE');
  return { date: localDate, ...sales, reputation, full };
}

// ===================================================================
// parseMetricsPage — extract KPIs from MercadoLibre Metrics page
// Page: https://www.mercadolibre.com.mx/metricas (filtered to "今天")
// Returns a sales-compatible object with pre-aggregated daily totals
// ===================================================================

function parseMetricsPage(text) {
  if (!text || typeof text !== "string") {
    return null;
  }

  // Only activate if the text looks like the metrics page
  if (!/绩效概览|总销售额/.test(text)) return null;

  // Extract the actual date from the page (e.g. "今天 26 7月 2026" or "今天26 7月 2026")
  var dateMatch = text.match(/今天\s*(\d{1,2})\s+(\d{1,2})月\s+(\d{4})/);
  var pageDate = null;
  if (dateMatch) {
    var d = parseInt(dateMatch[1], 10);
    var m = parseInt(dateMatch[2], 10);
    var y = parseInt(dateMatch[3], 10);
    pageDate = y + '-' + String(m).padStart(2, '0') + '-' + String(d).padStart(2, '0');
  }
  // Fallback: compute today in Mexico City timezone
  if (!pageDate) {
    try {
      var mx = new Intl.DateTimeFormat('sv-SE', {timeZone: 'America/Mexico_City'});
      pageDate = mx.format(new Date());
    } catch(e) {
      pageDate = new Date().toLocaleDateString('sv-SE');
    }
  }

  const data = {
    store: null,
    date: pageDate || new Date().toLocaleDateString('sv-SE'),
    orders: {
      count: 0,
      total_amount_mxn: 0,
      currency: "MXN",
      items: [],
    },
    shipping: { today: 0, upcoming: 0, in_transit: 0 },
    after_sales: { pending: 0 },
    products: [],
    alerts: [],
    raw_text_length: text.length,
  };

  // Gross sales: "总销售额 ... $ 2,652" or "$2,652"
  const salesMatch = text.match(/总销售额[^$]*\$\s*([\d,]+(?:\.\d+)?)/);
  if (salesMatch) {
    data.orders.total_amount_mxn = parseFloat(salesMatch[1].replace(/,/g, ""));
  }

  // Order count: "订单数量 ... 8"
  const orderMatch = text.match(/订单数量[^\d]*(\d+)/);
  if (orderMatch) {
    data.orders.count = parseInt(orderMatch[1], 10);
  }

  // Units sold: "已售件数 ... 9"
  const unitsMatch = text.match(/已售件数[^\d]*(\d+)/);
  const unitsSold = unitsMatch ? parseInt(unitsMatch[1], 10) : 0;

  // Returns: "退货数量 ... 0"
  const returnsMatch = text.match(/退货数量[^\d]*(\d+)/);
  if (returnsMatch) {
    data.after_sales.pending = parseInt(returnsMatch[1], 10);
  }

  // Cancelled: "已取消的销售数量 ... 0"
  const cancelMatch = text.match(/已取消的销售数量[^\d]*(\d+)/);
  const cancelled = cancelMatch ? parseInt(cancelMatch[1], 10) : 0;

  // Authorization code: "今日授权码： XXX"
  const authMatch = text.match(/今日授权码[：:]\s*(\S+)/);
  if (authMatch) {
    data.authorization_code = authMatch[1];
  }

  // Alerts
  if (data.orders.total_amount_mxn === 0) {
    data.alerts.push("no_sales_data");
  }
  if (cancelled > 0) {
    data.alerts.push(`cancelled_orders:${cancelled}`);
  }

  // Add a synthetic item for the metrics summary
  data.orders.items.push({
    sku: "METRICS",
    order_id: "metrics",
    price_mxn: data.orders.total_amount_mxn,
    quantity: unitsSold,
    status: "metrics_summary",
    product_name: `指标页汇总: ${data.orders.count}单 / ${unitsSold}件`
  });

  return data;
}

// ===================================================================
// parseSalesData — original sales-order parser (unchanged structure)
// ===================================================================

// Spanish month abbreviations → month number
const MESES = { ene:1, feb:2, mar:3, abr:4, may:5, jun:6, jul:7, ago:8, sep:9, oct:10, nov:11, dic:12 };

// Get today's date in Mexico City timezone
function getMexicoToday() {
  var fmt = new Intl.DateTimeFormat('es-MX', {
    day: '2-digit', month: 'short', timeZone: 'America/Mexico_City'
  });
  // output like "26 jul" or "26-jul" depending on Node version
  var parts = fmt.format(new Date()).replace(/\./g, '').split(/[\s-]+/);
  return { day: parseInt(parts[0], 10), month: MESES[parts[1].toLowerCase()] };
}

// Extract date from an order block; returns {day, month} or null
// Supports both Spanish ("26 jul") and Chinese ("26 7月") formats
function extractOrderDate(block) {
  // Spanish: "26 jul" → month name
  var m = block.match(/(\d{1,2})\s+(ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)\s+\d{2}:\d{2}/i);
  if (m) return { day: parseInt(m[1], 10), month: MESES[m[2].toLowerCase()] };
  // Chinese: "26 7月" → numeric month + 月
  m = block.match(/(\d{1,2})\s+(\d{1,2})月\s+\d{2}:\d{2}/);
  if (m) return { day: parseInt(m[1], 10), month: parseInt(m[2], 10) };
  return null;
}

/**
 * Core parser — works on plain text regardless of DOM or CLI mode
 */
function parseSalesData(text) {
  if (!text || typeof text !== "string") {
    return { error: "no_content", message: "Page content is empty" };
  }

  const data = {
    store: null,
    date: new Date().toLocaleDateString('sv-SE'),
    authorization_code: null,
    orders: {
      count: 0,
      total_amount_mxn: 0,
      currency: "MXN",
      items: [],
    },
    shipping: {
      today: 0,
      upcoming: 0,
      in_transit: 0,
    },
    after_sales: {
      pending: 0,
    },
    products: [],
    alerts: [],
    raw_text_length: text.length,
  };

  // ---- Authorization code (Spanish + Chinese) ----
  const authMatch = text.match(
    /(?:C[oó]digo de autorizaci[oó]n para hoy|今日授权码)[：:]\s*(\S+)/i
  );
  if (authMatch) {
    data.authorization_code = authMatch[1];
  } else {
    data.alerts.push("auth_code_missing");
  }

  // ---- Order count: computed from filtered items, not the page's "X ventas" summary
  // (page summary includes orders from all dates in view, e.g. "Últimos 2 meses")

  // ---- Shipping summary (Spanish + Chinese) ----
  const enviosHoy = text.match(/(?:Env[ií]os de hoy|今日发货)\s*(\d+)/i);
  const enviosProx = text.match(/(?:Pr[oó]ximos d[ií]as|未来几天)\s*(\d+)/i);
  const enviosTrans = text.match(/(?:En tr[aá]nsito|运输中)\s*(\d+)/i);
  if (enviosHoy) data.shipping.today = parseInt(enviosHoy[1], 10);
  if (enviosProx) data.shipping.upcoming = parseInt(enviosProx[1], 10);
  if (enviosTrans) data.shipping.in_transit = parseInt(enviosTrans[1], 10);

  // ---- After-sales ----
  const posventaMatch = text.match(/Posventa\s*(\d+)/i);
  if (posventaMatch) {
    data.after_sales.pending = parseInt(posventaMatch[1], 10);
  }

  // ---- Individual orders: extract price + SKU + product name + status ----
  // Pattern: status-text product-name quantity price SKU order-number
  // Examples from real page:
  // "Procesando en la bodega Llega hoy ... 1 unidad$ 219.12SKU: ZZD-MX-018 #200001..."

  // Strategy: split by "SKU:" boundaries, then parse each segment
  // NOTE: "SKU: ZZD-MX-XXX" appears at the END of the page card for each order.
  // When we split on "SKU:", block 0 = order 1 (no SKU — it's in the delimiter),
  // block i (i>=1) starts with SKU for order i (e.g. block 1 starts with "ZZD-MX-018"
  // which belongs to order 2). So for order 1 we peek at block 1's start.
  const orderBlocks = text.split(/SKU:\s*/i);
  const startIdx = orderBlocks.length > 0 && extractOrderDate(orderBlocks[0]) ? 0 : 1;
  const today = getMexicoToday();

  for (let i = startIdx; i < orderBlocks.length; i++) {
    const block = orderBlocks[i];

    // SKU: for block 0 (order 1), peek at next block's start.
    // For all other blocks, SKU is at current block's start.
    let sku = null;
    if (i === 0 && i + 1 < orderBlocks.length) {
      const nextBlock = orderBlocks[i + 1];
      const skuEnd = nextBlock.search(/\s/);
      sku = skuEnd > 0 ? nextBlock.slice(0, skuEnd) : null;
    } else {
      const skuEnd = block.search(/\s/);
      sku = skuEnd > 0 ? block.slice(0, skuEnd) : null;
    }
    // Skip blocks from page footer / navigation (no valid date)
    if (/Copyright|Mercado Libre®|Términos y condiciones|Trabaja con nosotros/i.test(block)) continue;

    // Extract order date; skip if no valid date found
    const orderDate = extractOrderDate(block);
    if (!orderDate) continue;

    // Only include today's orders
    if (orderDate.day !== today.day || orderDate.month !== today.month) continue;

    const item = {};

    // SKU (extracted from next block's start — see note above)
    item.sku = sku || null;

    // Order number (# followed by digits)
    const orderMatch = block.match(/#(\d+)/);
    if (orderMatch) item.order_id = orderMatch[1];

    // Price: $ xxx.xx pattern
    const priceMatch = block.match(/\$\s*([\d,]+\.?\d*)/);
    if (priceMatch) {
      item.price_mxn = parseFloat(priceMatch[1].replace(/,/g, ""));
      data.orders.total_amount_mxn += item.price_mxn;
    }

    // Quantity (Spanish "unidad/es" or Chinese "单位")
    const qtyMatch = block.match(/(\d+)\s*(?:unidad(?:es)?|单位)/i);
    if (qtyMatch) item.quantity = parseInt(qtyMatch[1], 10);

    // Product name: extract from the text BETWEEN status/noise keywords and price.
    const nameContext = block.slice(0, Math.max(block.indexOf("$"), 0)).trim();
    if (nameContext) {
      let cleanName = nameContext
        // Quantity
        .replace(/\d+\s*(?:unidad(?:es)?|单位)/gi, "")
        // Status keywords (Spanish + Chinese)
        .replace(/Procesando en la bodega|En camino|Entregado|Cancelado|在路上|在仓库处理中|已送达|已取消/gi, "")
        // Shipping / delivery text (Spanish + Chinese)
        .replace(/Llega\s+(?:hoy|ma[ñn]ana|entre\s+el\s+\d+\s+y\s+\d+\s+de\s+\w+|el\s+\w+\s+\d+\s+de\s+\w+)/gi, "")
        .replace(/明日送达|今天送达|星期\S+送达|跟踪物流/gi, "")
        // "Sin factura" / "无发票"
        .replace(/Sin factura(?: con RFC)?/gi, "")
        .replace(/无发票/gi, "")
        // "Seguir envío" / tracking
        .replace(/Seguir env[ií]o/gi, "")
        // Reputation text (Spanish + Chinese)
        .replace(/No afecta tu reputaci[oó]n/gi, "")
        .replace(/不影响您的信誉/gi, "")
        // Order numbers
        .replace(/#\d+/g, "")
        // Customer IDs
        .replace(/\b[A-Z]{2,}\d{5,}\b/g, "")
        // MercadoLibre internal ref
        .replace(/\bML\b\s*/g, "")
        // Messages / 消息
        .replace(/Mensajes/gi, "")
        .replace(/消息/gi, "")
        // FULL shipping notice (Spanish + Chinese)
        .replace(/Por ser un env[ií]o FULL[^.]*/gi, "")
        .replace(/由于采用FULL物流模式[^。]*。/gi, "")
        // Questions / misc
        .replace(/Recibiste \d+ pregunta[^.]*/gi, "")
        // Collapse whitespace
        .replace(/\s{2,}/g, " ")
        .trim();
      // Only use if it looks like a real product name (not just noise)
      if (cleanName && cleanName.length > 3) {
        item.product_name = cleanName.slice(0, 200);
      }
    }

    // Status (Spanish + Chinese)
    const statusMatch = block.match(
      /(Procesando en la bodega|En camino|Entregado|Cancelado|在仓库处理中|在路上|已送达|已取消)/i
    );
    if (statusMatch) item.status = statusMatch[1];

    if (item.sku || item.price_mxn) {
      data.orders.items.push(item);
    }
  }

  // Round total and set count from filtered items
  data.orders.total_amount_mxn =
    Math.round(data.orders.total_amount_mxn * 100) / 100;
  data.orders.count = data.orders.items.length;

  // ---- Product aggregation ----
  const productMap = {};
  for (const item of data.orders.items) {
    if (!item.sku) continue;
    if (!productMap[item.sku]) {
      productMap[item.sku] = {
        sku: item.sku,
        name: item.product_name || null,
        order_count: 0,
        total_amount: 0,
      };
    }
    productMap[item.sku].order_count += 1;
    productMap[item.sku].total_amount += item.price_mxn || 0;
  }
  data.products = Object.values(productMap).sort(
    (a, b) => b.order_count - a.order_count
  );

  // ---- Alerts ----
  if (data.orders.count === 0) {
    data.alerts.push("no_orders_found");
  }
  if (data.after_sales.pending > 0) {
    data.alerts.push("pending_after_sales");
  }
  if (data.orders.items.length === 0 && data.orders.count > 0) {
    data.alerts.push("order_detail_extraction_failed");
  }

  return data;
}

// ===================================================================
// parseReputationData — reputation page parser
// Page: https://www.mercadolibre.com.mx/reputacion?from=seller_menu
// ===================================================================

function parseReputationData(text) {
  if (!text || typeof text !== "string") {
    return { error: "no_content" };
  }

  const result = {};

  // ---- MercadoLíder level ----
  const levelMatch = text.match(/MercadoL[ií]der\s+(Gold|Platinum|Silver)/i);
  if (levelMatch) {
    result.level = levelMatch[0].trim(); // e.g. "MercadoLíder Gold"
  }

  // ---- Overall score (e.g. "6/7" after "Tu desempeño") ----
  const scoreMatch = text.match(/Tu desempeño\s*(\d+)\s*\/\s*(\d+)/i);
  if (scoreMatch) {
    result.score = `${scoreMatch[1]}/${scoreMatch[2]}`;
    result.score_numerator = parseInt(scoreMatch[1], 10);
    result.score_denominator = parseInt(scoreMatch[2], 10);
  }

  // ---- Period: last-60-days sales metrics ----
  // Patterns: "1,017 Ventas" / "979 Concretadas" / "$ 286,528 Facturado"
  const ventasPeriod = text.match(/([\d,]+)\s*Ventas/i);
  const concretadasMatch = text.match(/([\d,]+)\s*Concretadas/i);
  const facturadoMatch = text.match(/\$\s*([\d,]+)\s*Facturado/i);

  if (ventasPeriod || concretadasMatch || facturadoMatch) {
    result.period = {};
    if (ventasPeriod)
      result.period.sales = parseInt(ventasPeriod[1].replace(/,/g, ""), 10);
    if (concretadasMatch)
      result.period.completed = parseInt(concretadasMatch[1].replace(/,/g, ""), 10);
    if (facturadoMatch)
      result.period.facturado_mxn = parseFloat(facturadoMatch[1].replace(/,/g, ""));
  }

  // ---- Complaints breakdown ----
  // Page text pattern for each complaint type:
  //   "<desc>... X% Son N de tus ventas [con Envíos] Por debajo del Y% permitido"
  // The four types appear in order: Reclamos, Mediaciones, Canceladas por ti, Envíos incorrectos

  const complaints = {};
  const complaintDefs = [
    { key: 'reclamos_pct', countKey: 'reclamos_count', label: 'Reclamos' },
    { key: 'mediaciones_pct', countKey: 'mediaciones_count', label: 'Mediaciones' },
    { key: 'cancel_pct', countKey: 'cancel_count', label: 'Canceladas por ti' },
    { key: 'envio_err_pct', countKey: 'envio_err_count', label: 'Env\u00edos incorrectos' },
  ];

  // Split text into sections by complaint type names
  for (let i = 0; i < complaintDefs.length; i++) {
    const def = complaintDefs[i];
    // Find this section: from the complaint label to the next complaint label (or end)
    const startIdx = text.indexOf(def.label);
    if (startIdx === -1) continue;
    const nextIdx = i + 1 < complaintDefs.length
      ? text.indexOf(complaintDefs[i + 1].label, startIdx + def.label.length)
      : text.length;
    const section = text.slice(startIdx, nextIdx > startIdx ? nextIdx : text.length);

    // Extract percentage: first occurrence of "X%" or "X.X%" in this section
    const pctMatch = section.match(/(\d+(?:\.\d+)?)%/);
    if (pctMatch) complaints[def.key] = pctMatch[1] + '%';

    // Extract count: "Son N de tus ventas"
    const countMatch = section.match(/Son\s+(\d+)\s+de\s+tus\s+ventas/i);
    if (countMatch) complaints[def.countKey] = parseInt(countMatch[1], 10);
  }

  if (Object.keys(complaints).length > 0) {
    result.complaints = complaints;
  }

  // ---- New questions ----
  const newQMatch = text.match(/Recibiste\s+(\d+)\s+pregunta\s+nueva/i);
  if (newQMatch) {
    result.new_questions = parseInt(newQMatch[1], 10);
  }

  // ---- Problem products — table under "Productos con más problemas" ----
  // Look for the section heading then capture rows
  const probSection = text.match(
    /Productos con m[aá]s problemas\s*([\s\S]*?)(?=\n\s*\n|$)/
  );
  if (probSection) {
    const sectionText = probSection[1];
    // Each row: product name followed by problem count
    // Real format: "Product Name (3)" or "Product Name 3 problemas"
    const productLines = sectionText
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l.length > 0);

    const problemProducts = [];
    for (const line of productLines) {
      // Pattern: "Product Name (N)" or "Product Name - N problemas"
      const pMatch = line.match(/^(.+?)\s*[(-]\s*(\d+)\s*[)\s]/);
      if (pMatch) {
        problemProducts.push({
          name: pMatch[1].trim(),
          problems: parseInt(pMatch[2], 10),
        });
      } else {
        // Fallback: any line with a number at end
        const fallback = line.match(/^(.+?)\s+(\d+)\s*$/);
        if (fallback) {
          problemProducts.push({
            name: fallback[1].trim(),
            problems: parseInt(fallback[2], 10),
          });
        }
      }
    }
    if (problemProducts.length > 0) {
      result.problem_products = problemProducts;
    }
  }

  return result;
}

// ===================================================================
// parseFullData — FULL inbound shipments parser
// Page: https://myaccount.mercadolibre.com.mx/shipping/inbounds
// ===================================================================

function parseFullData(text) {
  if (!text || typeof text !== "string") {
    return { error: "no_content" };
  }

  const result = {};

  // ---- Pending shipments count ----
  const pendingMatch = text.match(
    /(\d+)\s*Env[ií]os\s+para\s+terminar\s+de\s+preparar/i
  );
  if (pendingMatch) {
    result.pending_count = parseInt(pendingMatch[1], 10);
  }

  // ---- Non-apt units (U. no aptas para Full) ----
  const notAptMatch = text.match(
    /U\.\s*no\s+aptas\s+para\s+Full\s*[:\s]*(\d+)/i
  );
  if (notAptMatch) {
    result.not_apt_total = parseInt(notAptMatch[1], 10);
  }

  // ---- Shipment rows ----
  // Each row pattern from real page:
  // #shipment_id | Declared/Apt | Date | Location | Status
  // Look for shipment IDs prefixed with #
  const shipmentBlocks = text.split(/(?=#\d{6,})/g);
  const shipments = [];

  for (const block of shipmentBlocks) {
    const idMatch = block.match(/^#(\d{6,})/);
    if (!idMatch) continue;

    const shipment = { id: idMatch[1] };

    // Status: one of the known values
    const statusMatch = block.match(
      /(En preparaci[oó]n|Procesamiento finalizado|Vencido|Finalizado)/i
    );
    if (statusMatch) shipment.status = statusMatch[1];

    // Declared / Apt for Full
    // Pattern like "Declared: 10 — Apt: 8" or "10 / 8"
    const declaredMatch = block.match(
      /(?:Declarados?|Declared)[:\s]*(\d+)/i
    );
    const aptMatch = block.match(
      /(?:Aptos?|Apt)[:\s]*(\d+)/i
    );
    if (declaredMatch) shipment.declared = parseInt(declaredMatch[1], 10);
    if (aptMatch) shipment.apt_for_full = parseInt(aptMatch[1], 10);

    // Appointment date — look for date patterns
    // Real: "20/07/2026" or "20-07-2026" or "lunes, 20 de julio de 2026"
    const dateMatch = block.match(
      /\b(\d{2}\/\d{2}\/\d{4})\b|\b(\d{2}-\d{2}-\d{4})\b/
    );
    if (dateMatch) {
      shipment.appointment = dateMatch[1] || dateMatch[2];
    } else {
      // Spanish date format: "lunes, 20 de julio de 2026"
      const spanDateMatch = block.match(
        /(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)[,.]?\s+\d{1,2}\s+de\s+\w+\s+de\s+\d{4}/i
      );
      if (spanDateMatch) shipment.appointment = spanDateMatch[0].trim();
    }

    // Location / warehouse
    const locationMatch = block.match(
      /(?:Ubicaci[oó]n|Location|Centro|Bodega|Warehouse)[:\s]*([^\n]{3,50})/i
    );
    if (locationMatch) {
      shipment.location = locationMatch[1].trim();
    }

    // Non-apt units within this shipment
    const blockNotApt = block.match(
      /U\.\s*no\s+aptas\s*[:\s]*(\d+)/i
    );
    if (blockNotApt) {
      shipment.not_apt_count = parseInt(blockNotApt[1], 10);
    }

    // Only include if it has at least one FULL-specific field
    // (filters out sales order IDs that also match #\d{6,})
    // Also require reasonable shipment ID length (FULL: ~6-10 digits; sales orders: 14-16 digits)
    if ((shipment.status || shipment.declared != null || shipment.apt_for_full != null
        || shipment.appointment || shipment.location || shipment.not_apt_count != null)
        && shipment.id.length <= 10) {
      shipments.push(shipment);
    }
  }

  if (shipments.length > 0) {
    result.shipments = shipments;
  }

  return result;
}

// ---- Entry point ----
// Detect execution context: browser (page exec) vs Node.js CLI
// Use require.main === module to distinguish direct execution from require()
const isDirectRun = typeof document !== "undefined"
  || (typeof require !== "undefined" && require.main === module);

if (typeof document !== "undefined") {
  // Running in browser via page exec
  const result = extractFromDOM();
  return JSON.stringify(result, null, 2);
} else if (isDirectRun && typeof process !== "undefined" && process.stdin) {
  // Running as Node.js CLI — read stdin
  let input = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    input += chunk;
  });
  process.stdin.on("end", () => {
    // Handle page content JSON wrapper from ziniao-cli
    try {
      const wrapped = JSON.parse(input);
      const pageContent =
        wrapped?.data?.data?.content ||
        wrapped?.content ||
        wrapped?.text ||
        input;
      const result = extractFromText(
        typeof pageContent === "string" ? pageContent : JSON.stringify(pageContent)
      );
      console.log(JSON.stringify(result, null, 2));
    } catch {
      const result = extractFromText(input);
      console.log(JSON.stringify(result, null, 2));
    }
  });
} else {
  // Being required as a module — export for programmatic use
  module.exports = {
    parseSalesData,
    parseReputationData,
    parseFullData,
    parseMetricsPage,
    extractFromDOM,
    extractFromText,
    mergeAll,
  };
}
