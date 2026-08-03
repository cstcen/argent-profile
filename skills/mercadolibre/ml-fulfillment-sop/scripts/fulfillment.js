#!/usr/bin/env node
/**
 * mercadolibre-fulfillment-sop 执行引擎
 * 管理 8 步货件创建流程的状态、命令生成和异常检测。
 *
 * 用法:
 *   node fulfillment.js status                          — 查看当前进度
 *   node fulfillment.js step <N>                        — 输出步骤 N 的命令
 *   node fulfillment.js step <N> --skus '[...]'         — 带 SKU 参数执行
 *   node fulfillment.js step <N> --format markdown      — Markdown 格式输出
 *   node fulfillment.js step <N> --push                 — 推送飞书卡片
 *   node fulfillment.js check --shipment-id <ID>        — 检查货件状态
 *   node fulfillment.js exceptions                      — 查看异常处理矩阵
 *   node fulfillment.js forbidden                       — 查看禁止事项
 *   node fulfillment.js help                            — 查看帮助
 *
 * 前置检查：飞书 CLI 配置
 *   使用 --push 或飞书多维表格功能前，请先运行 argent feishu-setup
 */

const { execSync } = require("child_process");

/** 检查飞书 CLI 是否已配置 */
function checkFeishu() {
  try {
    execSync("which lark-cli", { stdio: "ignore" });
  } catch {
    console.error("❌ 未检测到 lark-cli。飞书功能不可用。");
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
    console.error("⚠️  飞书状态检查失败——如果未使用 --push 或飞书表格功能，可忽略。");
  }
}

// 仅在使用 --push 或飞书相关参数时检查
const args = process.argv.slice(2);
const needsFeishu = args.includes("--push") || args.includes("--table");
if (needsFeishu) checkFeishu();

// ---- 页面选择器（基于 Andes 设计系统，2026-07-27 紫鸟真实页面验证）----
// 重要：ML 使用 Andes 组件库。querySelectorAll 不支持 :has-text()/:contains()——内容匹配必须用 textContent。
// 已验证项标注 ✅，待创建货件时校准项标注 ⚠️。
const SELECTORS = {
  1: {
    // ✅ 已验证
    table: 'table.andes-table',
    rows: 'table.andes-table tbody tr',
    statusCellIndex: 4,       // "Estado" 列（0-indexed）
    actionsCellIndex: 5,      // "Acciones" 列
    // 状态文本值（用于 textContent 匹配）
    statusValues: ['Vencido', 'En preparación', 'Pendiente de recepción', 'Procesamiento finalizado'],
    // 库容警告：.andes-card 中搜索 "liberar espacio"
    capacityWarning: '.andes-card',
    capacityWarningText: 'liberar espacio',
    // 数据提取脚本（page exec 用）
    extract: `(function(){
      var rows = document.querySelectorAll('table.andes-table tbody tr');
      var shipments = [];
      rows.forEach(function(row){
        var cells = row.querySelectorAll('td');
        if (cells.length < 6) return;
        var statusText = (cells[4].textContent || '').trim();
        var actionsText = (cells[5].textContent || '').trim();
        shipments.push({
          id: (cells[0].textContent || '').match(/#?(\\d{8})/)?.[1] || '',
          declared: (cells[1].textContent || '').trim(),
          appointment: (cells[2].textContent || '').trim(),
          status: statusText,
          action: actionsText
        });
      });
      return JSON.stringify(shipments);
    })();`,
  },
  2: {
    // ✅ 已确认组件类型（但库容满时按钮不显示）
    // Andes 按钮：a.andes-button 或 button.andes-button
    enviarBtnSelector: 'a.andes-button, button.andes-button',
    enviarBtnText: 'Enviar productos',
    note: '库容满时此按钮不显示，页面显示 "liberar espacio" 警告',
  },
  3: {
    // ⚠️ 待创建货件时验证
    skuInput: 'input[placeholder*="SKU"], input.andes-form-control[placeholder*="SKU"]',
    qtyInput: 'input[type="number"]',
    continuarBtnText: 'Continuar',
    confirmBtnText: 'Continuar con mi plan',
  },
  4: {
    // ✅ 已验证（2026-07-27 紫鸟真实预约页）
    // 页面：/shipping/inbounds/{id}/appointment-v2
    // 配送方式下拉
    shipmentDropdown: '[id*=shipment-type-selection], [class*=dropdown__trigger]',
    vehicleOptionText: 'Vehículo particular',
    // 日期选择（focus-ui-calendar 组件）
    dateInput: 'input[readonly][id^="_R_"]',          // 只读日期输入框（动态 ID）
    monthYear: '.focus-ui-datepicker__content__month-year',
    nextMonthBtn: '.focus-ui-datepicker__content__next_month_selector',
    prevMonthBtn: '.focus-ui-datepicker__content__back_month_selector',
    availableDay: 'div.day:not(.day--disabled)',       // 可选日期格子
    selectedDay: 'div.day--selected',
    // 时间槽（动态渲染在 .date-time-card-content 内）
    timeSlotContainer: '.date-time-card-content',
    // 确认按钮
    confirmBtn: 'button:not([disabled])',              // textContent === "Confirmar"
    // 自动化步骤
    automation: {
      // === 灰圈算法（2026-07-27 验证） ===
      // 目标：选择 30 天后的任意可用日期（不关心月份名）
      algorithm: `
1. 找灰圈: div.day--current（今日标记）
2. 从灰圈的下一格开始数，每格计 1（不论是否 disabled），数 31 格
3. 若当前视图不够 31 格（灰圈位置 + 31 > 42），PointerEvent 翻到下月
4. 翻月后灰圈会移到前月尾部位置，重新执行步骤 2
5. 选中后 fiber onClick(div.hour) 选时间 2:00
6. 主 Confirm（top>700 的按钮）提交——日历 Confirm 始终 disabled，但主按钮可绕过`,
      // === 交互模式 ===
      pointerEvent: 'dispatchEvent(PointerEvent("pointerover+enter+down+up",{bubbles:true,clientX,clientY,pointerId:1,pointerType:"mouse"})) + MouseEvent("click")',
      twoCall: '关键：PointerEvent 触发后需分两次 page exec——第一次触发事件，浏览器在调用间渲染 DOM，第二次与新元素交互',
      // === 操作步骤 ===
      step1_vehiculo: 'PointerEvent combobox → 下一调用找 [role=option] 含 "Vehículo" → click',
      step2_openCal: 'PointerEvent dateInput(_r_j_) → 下一调用日历已开',
      step3_nav: '灰圈 index+31 >= 42 → fiber onClick([aria-label="next month"]) → 下一调用验证月已变',
      step4_select: '灰圈 index+31 < 42 → fiber onClick(day) 选中 → fiber onClick(div.hour) 选时',
      step5_confirm: 'click 主按钮 button:not([disabled]):text("Confirmar")[top>700]',
      knownIssue: '日历格子 fiber onClick 选 DOM 但 React 不更新 → 日历 Confirm disabled。主 Confirm 可绕过提交',
    },
  },
  5: {
    // ✅ 已验证（2026-07-27）
    // Hub 页面展开包装确认区域 → 勾选两个复选框 → 点击 Confirmar
    confirmCheckbox: 'input[type="checkbox"]',          // top > 350 的两个复选框
    confirmBtn: 'button:not([disabled])',               // textContent === "Confirmar"
    note: '复选框为标准 input，直接 .click() 可用；但需先点击区域标题展开',
  },
  6: {
    // ✅ 已验证（2026-07-27 紫鸟真实标签页）
    // 页面：/shipping/inbounds/{id}/labeling
    // 从 Hub 页进入：a[href*="labeling"] + textContent === "Revisar"
    hubEntryCard: '[data-testid="link-identifiers"]',    // 初始：卡片本身 role="button"，点击进入
    hubEntryLink: 'a[href*="labeling"]',                // 二次：textContent === "Revisar"
    productExpandBtn: 'button',                          // textContent === "Producto"
    productCheckboxes: 'input[type=checkbox]',           // 父容器 .andes-checkbox__checkbox
    descargarBtn: 'button',                              // textContent === "Descargar etiquetas", 需 !disabled
    // 弹窗（modal）内元素
    modalPdfOption: 'button[inModal]',                   // textContent === "PDF"
    modalDownloadBtn: 'button[inModal]',                 // textContent === "Descargar"
    confirmBtn: 'button',                                // textContent === "Confirmar"
    // 🔴 关键 Pitfall：Andes 复选框是 React 受控组件
    // cb.click() 不会触发 React onChange，必须用 MouseEvent：
    //   cb.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, view: window}))
    // 复选框 DOM checked 不会更新，但 React 内部状态正确更新
    checkboxClickMethod: 'MouseEvent',
    checkboxCode: 'cb.dispatchEvent(new MouseEvent("click", {bubbles:true, cancelable:true, view:window}))',
  },
  7: {
    // ⚠️ 待创建货件时验证
    bultosOptionText: 'Bultos sin agrupar',
    boxQtyInput: 'input[type="number"]',
    downloadBtnText: 'Descargar',
  },
  8: {
    // ⚠️ 待创建货件时验证
    editarBtnText: 'Editar',
    cancelarReservaText: 'Cancelar reserva',
    confirmCancelText: 'Cancelar cita',
  },
};

// ---- textContent 辅助函数 ----
// Andes 组件不含业务 class，内容匹配必须用 textContent
function findByText(selector, text, exact = false) {
  const els = document.querySelectorAll(selector);
  for (const el of els) {
    const t = (el.textContent || '').trim();
    if (exact ? t === text : t.includes(text)) return el;
  }
  return null;
}

// ---- 步骤定义 ----
const STEPS = {
  1: {
    name: "前期准备",
    type: "read",
    description: "确认账号、产品、库容状态正常",
    commands: ({ storeId }) => [
      `ziniao-cli page visit --store-id ${storeId} --url "https://myaccount.mercadolibre.com.mx/shipping/inbounds" --wait-until networkidle`,
      `ziniao-cli page content --store-id ${storeId}`,
    ],
    checks: [
      "检查货件列表中是否有「Vencido」过期项",
      "检查「En preparación」货件数量",
      "检查页面是否有「liberar espacio」库容警告",
    ],
    forbidden: [
      "禁止超库容创建",
      "禁止为下架/违规 Listing 创建货件",
    ],
  },
  2: {
    name: "货件创建入口",
    type: "read",
    description: "导航到 FULL 管理页并点击 Enviar productos",
    commands: ({ storeId }) => [
      `ziniao-cli page visit --store-id ${storeId} --url "https://myaccount.mercadolibre.com.mx/shipping/inbounds" --wait-until networkidle`,
    ],
    checks: [
      "确认页面为官方 MercadoLibre 后台",
      "确认 Enviar productos 按钮可见",
    ],
  },
  3: {
    name: "选择产品与数量",
    type: "write",
    description: "搜索 SKU、填写数量、确认提交",
    commands: ({ storeId, skus }) => {
      const cmds = [
        `# ⚠️ 写操作 — 需要用户批准后才能执行`,
        `# SKU 列表: ${JSON.stringify(skus)}`,
        `ziniao-cli page visit --store-id ${storeId} --url "https://myaccount.mercadolibre.com.mx/shipping/inbounds" --wait-until networkidle`,
      ];
      if (skus && skus.length > 0) {
        cmds.push(`# 对每个 SKU 执行：搜索 → 输入数量 → Continuar`);
        for (const s of skus) {
          cmds.push(`#   SKU: ${s.sku} 数量: ${s.qty}`);
        }
      }
      cmds.push(`# 弹窗出现后点击「Continuar con mi plan actual」`);
      cmds.push(`# 等待系统响应（可能 10-30 秒）`);
      return cmds;
    },
    onFailure: [
      "超时/失败：停止重复提交",
      "等待 2 分钟后核查后台状态",
      "无有效货件：核对 SKU 信息、调整发货数量",
      "产品异常：修正 Listing 信息后再操作",
    ],
    forbidden: [
      "发货数量不得超出系统限制",
      "禁止重复创建同款同数量货件",
    ],
  },
  4: {
    name: "货件预约时间",
    type: "write",
    description: "选择自有车辆配送 + 30 天后的时间",
    commands: ({ storeId }) => [
      `# ⚠️ 写操作 — 需要用户批准后才能执行`,
      `ziniao-cli page visit --store-id ${storeId} --url "https://myaccount.mercadolibre.com.mx/shipping/inbounds" --wait-until networkidle`,
      `# 在「Elige cómo y cuándo vas a enviar」部分：`,
      `#   1. 选择「Vehículo particular」`,
      `#   2. 选择 30 天之后的任意时间`,
      `#   3. 点击「Continuar」`,
    ],
    forbidden: [
      "禁止手动修改仓库地址",
    ],
  },
  5: {
    name: "包装确认",
    type: "write",
    description: "确认产品包装合规",
    commands: ({ storeId }) => [
      `# ⚠️ 写操作 — 需要用户批准后才能执行`,
      `# 在「Revisa la preparación」部分：`,
      `#   1. 勾选确认框`,
      `#   2. 确认「Confirmo que el producto tiene un empaque correcto」`,
      `#   3. 点击「Continuar」`,
    ],
    onFailure: [
      "超时无结果：禁止重复提交",
      "先进入 FULL 列表页核查后台状态",
      "确认是否已生成货件记录",
    ],
  },
  6: {
    name: "标签下载",
    type: "read",
    description: "下载产品标签并按要求命名",
    commands: ({ storeId }) => [
      `ziniao-cli page visit --store-id ${storeId} --url "https://myaccount.mercadolibre.com.mx/shipping/inbounds" --wait-until networkidle`,
      `# 找到对应货件 → 点击「Descargar etiquetas」→ 「Descargar」`,
      `# 命名规则: Código_ML + SKU (例: YNWE47995+HW-MX-026-01)`,
    ],
    namingRule: "格式: Código ML + SKU。示例: YNWE47995+HW-MX-026-01",
  },
  7: {
    name: "打印箱唛",
    type: "write",
    description: "填写箱数、生成箱唛",
    commands: ({ storeId }) => [
      `# ⚠️ 写操作 — 需要用户批准后才能执行`,
      `# 在「Prepara bultos y/o pallets」部分：`,
      `#   1. 选择「Bultos sin agrupar en pallets」（散箱）`,
      `#   2. 填写箱数`,
      `#   3. 下载箱唛`,
    ],
    namingRule: "格式: 货件号 + SKU。示例: 71667033+HW-MX-026-01",
  },
  8: {
    name: "取消预约时间",
    type: "write",
    description: "仅预约下载箱唛，下载后必须取消",
    commands: ({ storeId }) => [
      `# ⚠️ 写操作 — 需要用户批准后才能执行`,
      `# 操作步骤:`,
      `#   1. 点击「Editar」`,
      `#   2. 选择「Cancelar reserva」`,
      `#   3. 确认「Cancelar cita」`,
    ],
    reason: "国内发货预约时间仅为下载箱唛，未预约时间无法下载箱唛。下载完箱唛后必须取消预约。",
  },
};

// ---- 异常处理矩阵（保持不变）----
const EXCEPTIONS = {
  timeout: {
    symptoms: ["超时", "timeout", "sin respuesta", "cargando"],
    action: "停止重复提交，等待 2 分钟后核查后台状态。无有效货件则核对 SKU 信息、调整数量重新创建。",
  },
  split_shipment: {
    symptoms: ["分仓", "分件", "múltiples envíos", "separado"],
    action: "按系统生成的多个货件 ID 分开打包、单独贴标、分别入仓。禁止混装合并。",
  },
  label_failure: {
    symptoms: ["标签", "etiqueta", "descargar", "código de barras"],
    action: "刷新页面或重新进入货件页下载，重新打印标签，确保条码可正常扫描入库。",
  },
};

// ---- 禁止事项（保持不变）----
const FORBIDDEN = [
  "禁止超系统推荐库容创建货件",
  "禁止多批次、多货件ID产品混装同一外箱",
  "禁止涂改、遮挡、模糊标签",
  "禁止重复创建同款同数量货件",
];

// ---- 辅助函数 ----

/** 将步骤信息格式化为 Markdown（参考 templates/report.md） */
function formatStepMarkdown(stepNum, step, output) {
  const typeLabel = step.type === "write" ? "写操作（需批准）" : "只读";
  const lines = [
    `# 步骤 ${stepNum}: ${step.name}`,
    "",
    `**类型**: ${typeLabel}`,
    `**描述**: ${step.description}`,
    "",
    "## 执行命令",
    "",
    "```",
    ...output.commands,
    "```",
  ];

  if (output.checks && output.checks.length > 0) {
    lines.push("", "## 检查项");
    for (const c of output.checks) {
      lines.push(`- ☐ ${c}`);
    }
  }

  if (output.onFailure && output.onFailure.length > 0) {
    lines.push("", "## 失败处理");
    for (const f of output.onFailure) {
      lines.push(`- ${f}`);
    }
  }

  if (output.forbidden && output.forbidden.length > 0) {
    lines.push("", "## 禁止事项");
    for (const f of output.forbidden) {
      lines.push(`- ⛔ ${f}`);
    }
  }

  if (output.namingRule) {
    lines.push("", `**命名规则**: ${output.namingRule}`);
  }

  if (output.reason) {
    lines.push("", `**说明**: ${output.reason}`);
  }

  return lines.join("\n");
}

/** 构建飞书推送卡片 JSON（对接 argent_cli/notification.py） */
function buildPushCard(stepNum, step, output) {
  const typeLabel = step.type === "write" ? "写操作（需批准）" : "只读";
  const skuCount = output.commands.filter((c) => c.startsWith("#   SKU:")).length;
  const contentLines = [
    `步骤 ${stepNum}: ${step.name}`,
    `类型: ${typeLabel}`,
    `描述: ${step.description}`,
    `命令数: ${output.commands.length}`,
    skuCount > 0 ? `SKU 数: ${skuCount}` : "",
  ].filter(Boolean);

  const elements = [
    { tag: "div", text: { tag: "lark_md", content: contentLines.join("\n") } },
  ];

  if (output.checks && output.checks.length > 0) {
    const checksText = output.checks.map((c) => `☐ ${c}`).join("\n");
    elements.push({ tag: "hr" });
    elements.push({ tag: "div", text: { tag: "lark_md", content: `**检查项**:\n${checksText}` } });
  }

  if (output.forbidden && output.forbidden.length > 0) {
    const forbiddenText = output.forbidden.map((f) => `⛔ ${f}`).join("\n");
    elements.push({ tag: "hr" });
    elements.push({ tag: "div", text: { tag: "lark_md", content: `**禁止事项**:\n${forbiddenText}` } });
  }

  return {
    msg_type: "interactive",
    card: {
      header: {
        title: { tag: "plain_text", content: `FULL 货件操作 - 步骤 ${stepNum}` },
      },
      elements,
    },
  };
}

// ---- CLI 入口 ----
function main() {
  const args = process.argv.slice(2);
  const cmd = args[0];

  if (!cmd || cmd === "help") {
    console.log(`用法:
  node fulfillment.js status                          查看当前进度
  node fulfillment.js step <N>                        输出步骤 N 的命令
  node fulfillment.js step <N> --skus '[...]'         带 SKU 参数执行
  node fulfillment.js step <N> --format markdown      以 Markdown 格式输出
  node fulfillment.js step <N> --push                 推送飞书卡片通知
  node fulfillment.js check --shipment-id <ID>        检查指定货件状态
  node fulfillment.js exceptions                      查看异常处理矩阵
  node fulfillment.js forbidden                       查看禁止事项
  node fulfillment.js help                            显示此帮助

使用 --store-id <ID> 指定店铺，或用 ziniao-cli store list 查看可用店铺。
`);
    return;
  }

  if (cmd === "status") {
    console.log(JSON.stringify({
      total_steps: 8,
      read_steps: [1, 2, 6],
      write_steps: [3, 4, 5, 7, 8],
      ready: true,
      hint: "请使用 --store-id 指定店铺，或运行 ziniao-cli store list 查看可用店铺",
    }, null, 2));
    return;
  }

  if (cmd === "exceptions") {
    console.log(JSON.stringify(EXCEPTIONS, null, 2));
    return;
  }

  if (cmd === "forbidden") {
    console.log(JSON.stringify(FORBIDDEN, null, 2));
    return;
  }

  if (cmd === "step") {
    const stepNum = parseInt(args[1], 10);
    if (!STEPS[stepNum]) {
      console.error(`错误: 无效步骤 "${stepNum}"。有效范围: 1-8`);
      process.exit(1);
    }
    const step = STEPS[stepNum];

    // 解析额外参数：--skus, --store-id, --shipment-id, --format, --push
    const params = { storeId: "<请传入 --store-id>" };
    let format = null;
    let pushMode = false;

    for (let i = 2; i < args.length; i++) {
      if (args[i] === "--store-id" && args[i + 1]) {
        params.storeId = args[i + 1];
        i++;
      } else if (args[i] === "--skus" && args[i + 1]) {
        try { params.skus = JSON.parse(args[i + 1]); } catch { console.error("错误: --skus 参数格式无效，请提供有效的 JSON 数组"); process.exit(1); }
        i++;
      } else if (args[i] === "--shipment-id" && args[i + 1]) {
        params.shipmentId = args[i + 1];
        i++;
      } else if (args[i] === "--format" && args[i + 1]) {
        format = args[i + 1];
        i++;
      } else if (args[i] === "--push") {
        pushMode = true;
      }
    }

    const output = {
      step: stepNum,
      name: step.name,
      type: step.type,
      description: step.description,
      requires_approval: step.type === "write",
      commands: step.commands(params),
      checks: step.checks || [],
      onFailure: step.onFailure || [],
      forbidden: step.forbidden || [],
      namingRule: step.namingRule || null,
      reason: step.reason || null,
      exceptionMatrix: EXCEPTIONS,
      selectors: SELECTORS[stepNum] || null,
    };

    // --format markdown：以 Markdown 格式输出
    if (format === "markdown") {
      console.log(formatStepMarkdown(stepNum, step, output));
      return;
    }

    // --push：输出飞书卡片 JSON
    if (pushMode) {
      console.log(JSON.stringify(buildPushCard(stepNum, step, output), null, 2));
      return;
    }

    // 默认 JSON 输出
    console.log(JSON.stringify(output, null, 2));
    return;
  }

  if (cmd === "check") {
    let shipmentId = null;
    for (let i = 1; i < args.length; i++) {
      if (args[i] === "--shipment-id" && args[i + 1]) {
        shipmentId = args[i + 1];
        break;
      }
    }
    if (!shipmentId) {
      console.error("错误: 请提供 --shipment-id 参数指定要检查的货件 ID");
      process.exit(1);
    }
    console.log(JSON.stringify({
      action: "check_shipment",
      shipment_id: shipmentId,
      commands: [
        `ziniao-cli page visit --store-id <ID> --url "https://myaccount.mercadolibre.com.mx/shipping/inbounds" --wait-until networkidle`,
        `ziniao-cli page content --store-id <ID>`,
        `# 在页面中搜索货件号 ${shipmentId}`,
        `# 确认状态: En preparación / Procesamiento finalizado / Vencido`,
      ],
    }, null, 2));
    return;
  }

  console.error(`错误: 未知命令 "${cmd}"。请使用 "node fulfillment.js help" 查看可用命令`);
  process.exit(1);
}

main();
