#!/usr/bin/env python3
"""FULL 货件编排器 — Agent 驱动的结构化封装（替代 poll-fulfillment.sh）。

架构（与旧 bash 的本原区别）:
    Cron 触发 Agent (no_agent=false)
      → execute_code(fulfillment_orchestrator.py)
        → 脚本内部: ziniao-cli / lark-cli 调用的结构化封装
        → 每步有: 重试逻辑、选择器 fallback 链、超时处理
        → 返回结构化 JSON 给 Agent
      → Agent 根据返回值做高层决策

用法:
    python3 fulfillment_orchestrator.py --mode inspect
        自检: 加载 fulfillment.js SELECTORS、检查配置完整性（无副作用）
    python3 fulfillment_orchestrator.py --mode dry-run [--record-id recXXX]
        只执行只读步骤 1/2/6，写步骤(3/4/5/7/8)全部跳过
    python3 fulfillment_orchestrator.py --mode full [--allow-write]
        全流程编排。写步骤默认拒绝，需 --allow-write 才执行
    python3 fulfillment_orchestrator.py --mode step --step N [--allow-write]
        从指定步骤继续（Agent 确认后恢复执行）

返回: stdout 单行 JSON（progress 日志走 stderr，保证 stdout 纯净）
    {
      "status": "success|partial|failed|no_pending|needs_approval",
      "record_id": "recXXXX", "shipment_id": "12345678", "sku": "HW-MX-026-01",
      "completed_steps": [1,2,3], "failed_step": null,
      "error": {"step":4,"type":"selector_not_found","message":"...","recovery_attempted":[...]},
      "files_uploaded": {"产品标签":"xxx.pdf","箱唛":"yyy.pdf"}
    }

凭据: 一律来自环境变量（FEISHU_BASE_TOKEN / FEISHU_TABLE_ID / ML_STORE_ID /
ML_STORE_NAME / FEISHU_USER_ID / ZINIAO_DL），或本地 ~/.hermes/scripts/fulfillment.env。
本文件不包含任何硬编码凭据。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
FULFILLMENT_JS = SCRIPT_DIR / "fulfillment.js"

ML_BASE_URL = "https://myaccount.mercadolibre.com.mx"
INBOUNDS_URL = f"{ML_BASE_URL}/shipping/inbounds"

# 写步骤（会修改卖家后台数据）——默认 allow_write=False，Agent 确认后才执行
WRITE_STEPS: tuple[int, ...] = (3, 4, 5, 7, 8)
READ_STEPS: tuple[int, ...] = (1, 2, 6)

STEP_NAMES: dict[int, str] = {
    1: "前期准备",
    2: "货件创建入口",
    3: "选择产品与数量",
    4: "货件预约时间",
    5: "包装确认",
    6: "标签下载",
    7: "打印箱唛",
    8: "取消预约时间",
}


# ────────────────────────────────────────────────
# 配置
# ────────────────────────────────────────────────

@dataclass
class Config:
    """运行配置。优先环境变量，其次本地 env 文件（~/.hermes/scripts/fulfillment.env）。"""
    base_token: str = ""
    table_id: str = ""
    store_id: str = ""
    store_name: str = ""
    feishu_user: str = ""
    ziniaodl: str = ""
    ml_base_url: str = ML_BASE_URL

    @property
    def feishu_ready(self) -> bool:
        return bool(self.base_token and self.table_id)

    @property
    def browser_ready(self) -> bool:
        return bool(self.store_id and self.store_name)


def _default_env_file() -> Path:
    raw = os.environ.get("FULFILLMENT_ENV_FILE", "~/.hermes/scripts/fulfillment.env")
    return Path(raw).expanduser()


def load_config(env_file: Optional[Path] = None) -> Config:
    """加载配置：环境变量优先，env 文件兜底（文件为 KEY=VALUE 行，仅本地存在）。"""
    env: dict[str, str] = {}
    path = env_file or _default_env_file()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")

    def pick(name: str) -> str:
        return os.environ.get(name) or env.get(name, "")

    return Config(
        base_token=pick("FEISHU_BASE_TOKEN"),
        table_id=pick("FEISHU_TABLE_ID"),
        store_id=pick("ML_STORE_ID"),
        store_name=pick("ML_STORE_NAME"),
        feishu_user=pick("FEISHU_USER_ID"),
        ziniaodl=pick("ZINIAO_DL") or str(Path.home() / "Library/Application Support/ziniaobrowserdatas/ziniao browser"),
    )


# ────────────────────────────────────────────────
# 异常
# ────────────────────────────────────────────────

class StepError(Exception):
    """步骤执行失败，携带结构化信息供 JSON 输出。"""

    def __init__(self, step: int, err_type: str, message: str,
                 recovery_attempted: Optional[list[str]] = None) -> None:
        super().__init__(message)
        self.step = step
        self.err_type = err_type          # selector_not_found | timeout | cli_error | parse_error | business
        self.message = message
        self.recovery_attempted = recovery_attempted or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "type": self.err_type,
            "message": self.message,
            "recovery_attempted": self.recovery_attempted,
        }


# ────────────────────────────────────────────────
# 选择器管理：从 fulfillment.js 导入 SELECTORS + fallback 链
# ────────────────────────────────────────────────

# 步骤 → {逻辑动作: (fulfillment.js 选择器键名 | None, [后备选择器...])}
# 链首来自 fulfillment.js SELECTORS（单一事实源，不重复定义），
# 后备选择器为本脚本补充的替代方案（历史验证过的等价写法）。
STEP_FALLBACKS: dict[int, dict[str, tuple[Optional[str], list[str]]]] = {
    1: {
        "table": ("table", []),
        "rows": ("rows", []),
    },
    2: {
        "enviar_btn": ("enviarBtnSelector", []),
    },
    3: {
        "sku_input": (None, ['input[placeholder*="Buscar por producto"]',
                             'input[placeholder*="SKU"]',
                             'input[placeholder*="搜索"]']),
        "qty_input": (None, ['input[id^="_r_"][class*="andes-form-control"]',
                             'input[type="number"]']),
        "continuar_btn": (None, ["button"]),          # textContent === "Continuar" && !disabled
        "plan_modal_btn": (None, ["button"]),         # textContent === "Continuar con mi plan actual"
    },
    4: {
        "shipment_dropdown": ("shipmentDropdown",
                              ["#shipment-type-selection-dropdown-id-trigger",
                               '[role="combobox"]']),
        "vehicle_option": (None, ['[role="option"]']),  # textContent 含 "Vehículo"
        "date_input": ("dateInput", ['input[readonly][id^="_r_"]']),
        "next_month": ("nextMonthBtn", ['[aria-label="next month"]']),
        "day": ("availableDay", ["div.day"]),
        "hour": (None, ["div.hour"]),
        "confirm_btn": ("confirmBtn", ["button"]),    # textContent === "Confirmar"
    },
    5: {
        "checkboxes": ("confirmCheckbox", ['input[type="checkbox"]']),
        "confirm_btn": ("confirmBtn", ["button"]),    # textContent === "Confirmar"
    },
    6: {
        "hub_entry": ("hubEntryCard", ['a[href*="labeling"]']),
        "product_expand": ("productExpandBtn", ["button"]),  # textContent === "Producto"
        "checkboxes": ("productCheckboxes", ['input[type="checkbox"]']),
        "descargar_btn": ("descargarBtn", ["button"]),       # "Descargar etiquetas" && !disabled
        "modal_download": ("modalDownloadBtn", ["button"]),  # 弹窗内 "Descargar"
        "confirm_btn": ("confirmBtn", ["button"]),           # "Confirmar" && !disabled
    },
    7: {
        "qty_input": (None, ["#labelsQuantity", 'input[type="number"]']),
        "andes_checkbox": (None, ["label.andes-checkbox"]),  # 至少 3 个，勾选 #2/#3
        "generate_btn": (None, ["button"]),                  # "Generar etiquetas" && !disabled
        "download_all": (None, ["button"]),                  # textContent 含 "Descarga todas"
        "fragile_checkbox": (None, ['[data-testid="checkbox-fragils-consolidation"]']),
        "continuar_btn": (None, ["button"]),                 # "Continuar" && !disabled
    },
    8: {
        "editar_link": (None, ["a"]),                # 第 2 个 textContent === "Editar"
        "cancelar_reserva": (None, ["button"]),      # "Cancelar reserva"
        "cancelar_cita": (None, ["button"]),         # "Cancelar cita"
    },
}


class Selectors:
    """从 fulfillment.js 导入 SELECTORS，并按 STEP_FALLBACKS 组装 fallback 链。"""

    def __init__(self, js_path: Path = FULFILLMENT_JS) -> None:
        self.js_path = js_path
        self.js_selectors: dict[str, Any] = self._load(js_path)
        self.chains: dict[int, dict[str, list[str]]] = self._build_chains()

    @staticmethod
    def _load(js_path: Path) -> dict[str, Any]:
        """通过 node require 导入 fulfillment.js 的 SELECTORS 常量。

        fulfillment.js 在文件末尾已做 require.main 守卫并 module.exports，
        因此可安全 require 而不触发 CLI 入口。
        """
        code = (
            "const m=require(%s);"
            "process.stdout.write(JSON.stringify(m.SELECTORS||{}));"
            % json.dumps(str(js_path))
        )
        proc: Optional[subprocess.CompletedProcess[str]] = None
        for attempt in range(2):
            try:
                proc = subprocess.run(
                    ["node", "-e", code],
                    capture_output=True, text=True, timeout=20,
                )
            except subprocess.TimeoutExpired as exc:
                raise StepError(0, "cli_error", f"node require 导入 SELECTORS 超时: {js_path}") from exc
            if proc.returncode == 0 and proc.stdout.strip():
                try:
                    return json.loads(proc.stdout)
                except json.JSONDecodeError as exc:
                    raise StepError(0, "parse_error",
                                    f"SELECTORS JSON 解析失败: {proc.stdout[:200]}") from exc
            time.sleep(1)
        assert proc is not None
        raise StepError(0, "cli_error",
                        f"node require 导入 SELECTORS 失败 (exit={proc.returncode}): "
                        f"{proc.stderr.strip()[:300]}")

    def _build_chains(self) -> dict[int, dict[str, list[str]]]:
        chains: dict[int, dict[str, list[str]]] = {}
        for step, actions in STEP_FALLBACKS.items():
            chains[step] = {}
            js_sel = self.js_selectors.get(str(step), {})
            for action, (js_key, alts) in actions.items():
                chain: list[str] = []
                if js_key:
                    primary = js_sel.get(js_key)
                    if isinstance(primary, str) and primary.strip():
                        chain.append(primary)
                chain.extend(a for a in alts if a not in chain)
                chains[step][action] = chain or [""]
        return chains

    def chain(self, step: int, action: str) -> list[str]:
        return self.chains[step][action]

    def summary(self) -> dict[str, Any]:
        return {
            "js_selectors_steps": sorted(self.js_selectors.keys()),
            "fallback_actions": {str(s): list(a.keys()) for s, a in self.chains.items()},
            "chain_counts": {str(s): {a: len(c) for a, c in acts.items()}
                             for s, acts in self.chains.items()},
        }


# ────────────────────────────────────────────────
# ziniao-cli 封装
# ────────────────────────────────────────────────

class ZiniaoClient:
    """ziniao-cli 统一封装：自动解析 data.data.result、重试、超时。"""

    def __init__(self, cfg: Config, selectors: Optional[Selectors] = None) -> None:
        self.cfg = cfg
        self._selectors = selectors

    def _run(self, args: list[str], timeout: int = 60, retries: int = 3,
             step: int = 0, wait_after: float = 0.0) -> str:
        """执行 ziniao-cli 命令并返回 stdout；CLI 级失败自动重试。"""
        last_err = ""
        for attempt in range(retries):
            try:
                proc = subprocess.run(
                    ["ziniao-cli", *args],
                    capture_output=True, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                last_err = f"ziniao-cli 超时({timeout}s): {' '.join(args[:2])}"
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                continue
            if proc.returncode == 0:
                if wait_after:
                    time.sleep(wait_after)
                return proc.stdout
            last_err = proc.stderr.strip()[:300] or f"exit={proc.returncode}"
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
        raise StepError(step, "cli_error", f"ziniao-cli 调用失败: {last_err}",
                        recovery_attempted=[f"retry_{retries}x"])

    # -- 页面操作 --

    def open_store(self, step: int = 1) -> None:
        self._run(["store", "open", "--name", self.cfg.store_name, "--headless"],
                  timeout=90, step=step)

    def close_store(self) -> None:
        try:
            self._run(["store", "close", "--id", self.cfg.store_id], timeout=30, retries=1)
        except StepError:
            pass  # 关闭失败不影响主流程

    def visit(self, url: str, step: int = 0, wait_until: str = "networkidle",
              wait_after: float = 0.0) -> None:
        self._run(["page", "visit", "--store-id", self.cfg.store_id,
                   "--url", url, "--wait-until", wait_until],
                  timeout=90, step=step, wait_after=wait_after)

    def content(self, step: int = 0) -> str:
        return self._run(["page", "content", "--store-id", self.cfg.store_id],
                         timeout=60, step=step)

    def exec_js(self, script: str, step: int = 0, retries: int = 3,
                wait_after: float = 0.0) -> str:
        """执行页面 JS，自动解析 data.data.result；返回 result 字符串。"""
        out = self._run(["page", "exec", "--store-id", self.cfg.store_id,
                         "--script", script],
                        timeout=60, retries=retries, step=step, wait_after=wait_after)
        try:
            payload = json.loads(out)
            result = payload["data"]["data"].get("result", "")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StepError(step, "parse_error",
                            f"page exec 输出解析失败: {out[:200]}",
                            recovery_attempted=[f"retry_{retries}x"]) from exc
        return str(result) if result is not None else ""

    def exec_with_fallback(self, step: int, action: str, js_builder: Callable[[str], str],
                           expected: str, retries: int = 3,
                           wait_after: float = 0.0) -> str:
        """按 fallback 链执行：链首来自 fulfillment.js，失败依次尝试后备选择器。

        返回实际 result；全部失败抛 StepError（含 recovery_attempted）。
        """
        chain = self._selectors.chain(step, action) if self._selectors else [""]
        recovery: list[str] = []
        for idx, selector in enumerate(chain):
            script = js_builder(selector)
            try:
                result = self.exec_js(script, step=step, retries=retries,
                                      wait_after=wait_after)
            except StepError as exc:
                recovery.extend(exc.recovery_attempted)
                continue
            if result == expected:
                return result
            if result != "notfound":
                # 元素找到但状态不符（如按钮 disabled）——换选择器无意义，直接报业务错误
                raise StepError(step, "business",
                                f"[{action}] 元素状态异常: {result}",
                                recovery_attempted=recovery)
            if idx < len(chain) - 1:
                recovery.append(f"alt_selector:{action}@{idx + 1}")
        raise StepError(step, "selector_not_found",
                        f"[{action}] 未找到目标元素（fallback 链 {len(chain)} 个选择器全部失败）",
                        recovery_attempted=recovery)

    def wait_for(self, js_builder: Callable[[str], str], selector: str,
                 expected: str, timeout_s: int, interval_s: float, step: int,
                 action: str) -> str:
        """轮询等待条件成立（用于 React 异步渲染 / 页面跳转）。"""
        deadline = time.monotonic() + timeout_s
        result = ""
        while time.monotonic() < deadline:
            try:
                result = self.exec_js(js_builder(selector), step=step, retries=1)
            except StepError:
                result = ""
            if result == expected:
                return result
            time.sleep(interval_s)
        raise StepError(step, "timeout",
                        f"[{action}] 等待超时({timeout_s}s): 期望 {expected!r}，最后状态 {result!r}",
                        recovery_attempted=[f"poll_{int(timeout_s / interval_s)}x"])

    def two_phase_exec(self, phase1: Callable[[], str], phase2: str,
                       wait: float = 1.0, step: int = 0) -> str:
        """React 两步渲染：先触发事件（PointerEvent），等浏览器渲染，再与新建 DOM 交互。

        Andes 组件的关键模式：PointerEvent 触发后 DOM 在调用间渲染，
        必须分两次 page exec —— 第二次调用才能拿到新元素。
        """
        phase1()
        time.sleep(wait)
        return self.exec_js(phase2, step=step)

    def visit_plan_page(self, path: str, shipment_id: str, step: int) -> None:
        """访问货件子页面：使用 location.href JS 导航（page visit 在 ML 内部跳转会触发 chrome-error）。"""
        # 先用当前 URL 提取 inbound ID，拼完整 URL 后用 location.href 跳转
        path_escaped = path.replace("'", "\\'")
        url_js = (
            "(function(){"
            "var m=location.href.match(/\\/inbounds\\/(\\d+)/);"
            "if(m){return 'https://myaccount.mercadolibre.com.mx/shipping/inbounds/'+m[1]+'/" + path_escaped + "';}"
            "return '';})();"
        )
        url = self.exec_js(url_js, step=step, retries=1)
        if not url or url == "null":
            if shipment_id:
                url = f"{ML_BASE_URL}/shipping/inbounds/{shipment_id}/{path}"
            else:
                url = f"{ML_BASE_URL}/shipping/{path}"
        # JS 导航
        self.exec_js(
            "(function(){location.href='" + url + "';return 'navigating';})();",
            step=step, wait_after=3.0)


# ────────────────────────────────────────────────
# lark-cli 封装（飞书多维表格 + 消息）
# ────────────────────────────────────────────────

class FeishuClient:
    """lark-cli 封装：Pending 记录查询、字段更新、附件上传、消息推送。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _run(self, args: list[str], timeout: int = 60, retries: int = 2) -> str:
        last_err = ""
        for attempt in range(retries):
            try:
                proc = subprocess.run(["lark-cli", *args],
                                      capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                last_err = f"lark-cli 超时({timeout}s)"
                if attempt < retries - 1:
                    time.sleep(1)
                continue
            if proc.returncode == 0:
                return proc.stdout
            last_err = proc.stderr.strip()[:300] or f"exit={proc.returncode}"
            if attempt < retries - 1:
                time.sleep(1)
        raise StepError(0, "cli_error", f"lark-cli 调用失败: {last_err}",
                        recovery_attempted=[f"retry_{retries}x"])

    def list_pending(self) -> list[dict[str, Any]]:
        """查询 状态=Pending 且 就绪=true 的记录（与旧 bash 相同解析）。"""
        if not self.cfg.feishu_ready:
            raise StepError(0, "business", "飞书未配置（缺 FEISHU_BASE_TOKEN/FEISHU_TABLE_ID）")
        filter_json = json.dumps({"logic": "and", "conditions": [["状态", "==", "Pending"]]})
        out = self._run(["base", "+record-list",
                         "--base-token", self.cfg.base_token,
                         "--table-id", self.cfg.table_id,
                         "--filter-json", filter_json,
                         "--format", "json"], timeout=90)
        try:
            d = json.loads(out)
            fields = d["data"]["fields"]
            rows = d["data"]["data"]
            ids = d["data"]["record_id_list"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StepError(0, "parse_error", f"record-list 输出解析失败: {out[:200]}") from exc

        records: list[dict[str, Any]] = []
        for i, row in enumerate(rows):
            f = dict(zip(fields, row))
            ready = f.get("就绪") is True
            status = f.get("状态")
            statuses = status if isinstance(status, list) else [status]
            if ready and any("Pending" in str(s) for s in statuses if s is not None):
                records.append({
                    "record_id": ids[i],
                    "sku": str(f.get("SKU", "") or ""),
                    "name": str(f.get("品名", "") or ""),
                    "qty": str(f.get("数量", "") or ""),
                    "box": str(f.get("箱数", "") or ""),
                    "shipment_id": str(f.get("货件号") or ""),
                })
        return records

    def update_field(self, record_id: str, field_name: str, value: Any) -> None:
        """best-effort：Base 更新失败只记日志，不阻断浏览器主流程。"""
        if not self.cfg.feishu_ready:
            return
        payload = json.dumps({"record_id_list": [record_id],
                              "patch": {field_name: value}})
        try:
            self._run(["base", "+record-batch-update",
                       "--base-token", self.cfg.base_token,
                       "--table-id", self.cfg.table_id,
                       "--json", payload])
        except StepError as exc:
            print(f"[lark-cli] 更新字段 {field_name} 失败(忽略): {exc.message}",
                  file=sys.stderr, flush=True)

    def update_step(self, record_id: str, step_label: str) -> None:
        self.update_field(record_id, "当前步骤", step_label)

    def upload_attachment(self, record_id: str, field_name: str, file_path: str) -> None:
        if not self.cfg.feishu_ready:
            raise StepError(0, "business", "飞书未配置，无法上传附件")
        self._run(["base", "+record-upload-attachment",
                   "--base-token", self.cfg.base_token,
                   "--table-id", self.cfg.table_id,
                   "--record-id", record_id,
                   "--field-id", field_name,
                   "--file", file_path], timeout=120)

    def send_message(self, text: str) -> None:
        if not (self.cfg.feishu_ready and self.cfg.feishu_user):
            return
        try:
            self._run(["im", "+messages-send",
                       "--user-id", self.cfg.feishu_user,
                       "--text", text], timeout=60, retries=1)
        except StepError:
            pass  # 消息推送失败不阻断主流程


# ────────────────────────────────────────────────
# JS 片段（源自 poll-fulfillment.sh 验证过的实现）
# ────────────────────────────────────────────────

def _js_str(s: str) -> str:
    """把 Python 字符串安全嵌入 JS 单引号字符串。"""
    return s.replace("\\", "\\\\").replace("'", "\\'")


# --- 步骤 1：提取货件列表 ---
JS_EXTRACT_SHIPMENTS = """(function(){
  var rows = document.querySelectorAll('table.andes-table tbody tr');
  var shipments = [];
  rows.forEach(function(row){
    var cells = row.querySelectorAll('td');
    if (cells.length < 6) return;
    shipments.push({
      id: (cells[0].textContent || '').match(/#?(\\d{8})/)?.[1] || '',
      declared: (cells[1].textContent || '').trim(),
      appointment: (cells[2].textContent || '').trim(),
      status: (cells[4].textContent || '').trim(),
      action: (cells[5].textContent || '').trim()
    });
  });
  var warning = null;
  var cards = document.querySelectorAll('.andes-card');
  for (var i = 0; i < cards.length; i++) {
    if (cards[i].textContent.indexOf('liberar espacio') >= 0) { warning = 'capacity_warning'; break; }
  }
  return JSON.stringify({shipments: shipments, capacity_warning: warning});
})();"""


def js_click_button(text: str, selector: str, require_enabled: bool = True) -> str:
    """按 textContent 精确匹配按钮并点击；返回 'clicked' | 'notfound'。"""
    enabled = " && !els[i].disabled" if require_enabled else ""
    return (
        "(function(){var sels=['" + selector + "'];"
        "for(var s=0;s<sels.length;s++){var els=document.querySelectorAll(sels[s]);"
        "for(var i=0;i<els.length;i++){"
        "if(els[i].textContent.trim()==='" + _js_str(text) + "'" + enabled + "){"
        "els[i].click();return 'clicked';}}}return 'notfound';})();"
    )


def js_click_button_contains(text: str, selector: str) -> str:
    """textContent 包含匹配（如 Descarga todas）。"""
    return (
        "(function(){var els=document.querySelectorAll('" + selector + "');"
        "for(var i=0;i<els.length;i++){"
        "if(els[i].textContent.indexOf('" + _js_str(text) + "')>=0){"
        "els[i].click();return 'clicked';}}return 'notfound';})();"
    )


def js_pointer_click(selector: str, extra_pre: str = "") -> str:
    """已验证的 PointerEvent 全序列 + MouseEvent click。extra_pre 在事件前执行。"""
    return (
        "(function(){var el=document.querySelector('" + selector + "');"
        "if(!el)return 'notfound';" + extra_pre +
        "var r=el.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;"
        "el.dispatchEvent(new PointerEvent('pointerover',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse'}));"
        "el.dispatchEvent(new PointerEvent('pointerenter',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse'}));"
        "el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1}));"
        "el.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:0}));"
        "el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,view:window}));"
        "return 'clicked';})();"
    )


# --- 步骤 3：搜索 SKU（React 受控 input：native setter + input 事件）---
def js_search_sku(sku: str, selector: str) -> str:
    return (
        "(function(){var i=document.querySelector('" + selector + "');"
        "if(!i)return 'notfound';"
        "var ns=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
        "ns.call(i,'" + _js_str(sku) + "');"
        "i.dispatchEvent(new Event('input',{bubbles:true}));"
        "i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));"
        "return 'searched';})();"
    )


def js_fill_qty(qty: str, selector: str) -> str:
    return (
        "(function(){var q=document.querySelector('" + selector + "');"
        "if(!q)return 'notfound';"
        "q.click();q.focus();"
        "var ns=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
        "ns.call(q,'" + _js_str(qty) + "');"
        "q.dispatchEvent(new Event('input',{bubbles:true}));"
        "q.dispatchEvent(new Event('change',{bubbles:true}));"
        "return 'filled';})();"
    )


JS_EXTRACT_ML_CODE = """(function(){
  var tds = document.querySelectorAll('td');
  for (var i = 0; i < tds.length; i++) {
    var t = tds[i].textContent.trim();
    if (/^[A-Z]{4}[0-9]+$/.test(t) || /^ML[UB][0-9]+$/.test(t)) return t;
  }
  return 'UNKNOWN';
})();"""

JS_EXTRACT_SHIPMENT_ID = """(function(){
  var m = location.href.match(/\\/plans\\/(\\d+)/);
  if (m) return m[1];
  var m2 = location.href.match(/\\/(\\d{8})\\//);
  if (m2) return m2[1];
  return 'UNKNOWN';
})();"""

# --- 步骤 5：包装确认（Andes checkbox 必须 MouseEvent）---
JS_PACKAGE_CONFIRM = """(function(){
  var cbs = document.querySelectorAll('input[type=checkbox]');
  if (!cbs.length) return 'notfound';
  cbs.forEach(function(cb){
    cb.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));
  });
  var b = document.querySelectorAll('button');
  for (var i = 0; i < b.length; i++) {
    if (b[i].textContent.trim() === 'Confirmar') { b[i].click(); return 'done'; }
  }
  return 'noconfirm';
})();"""

# --- 步骤 6：标签页复选框（combined：checked + change + MouseEvent + fiber onChange）---
JS_LABEL_CHECKBOXES = """(function(){
  var cbs = document.querySelectorAll('input[type=checkbox]');
  for (var i = 0; i < cbs.length; i++) {
    var cb = cbs[i];
    if (!cb.offsetParent) continue;
    cb.checked = true;
    cb.dispatchEvent(new Event('change', {bubbles: true}));
    cb.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
  }
  var containers = document.querySelectorAll('[data-andes-checkbox-container]');
  containers.forEach(function(c){
    var k = Object.keys(c).find(function(x){ return x.startsWith('__reactFiber'); });
    if (!k) return;
    var n = c[k];
    for (var j = 0; j < 15 && n; j++) {
      if (n.memoizedProps && n.memoizedProps.onChange) {
        n.memoizedProps.onChange({target:{checked:true},preventDefault:function(){},stopPropagation:function(){}});
        break;
      }
      n = n.return;
    }
  });
  return 'checked:' + cbs.length + ' inputs, ' + containers.length + ' andes';
})();"""

# --- 步骤 6/7：弹窗内点击（PDF 弹窗 / 箱唛弹窗）---
def js_modal_click(title_contains: str, btn_text: str) -> str:
    """在含 title_contains 的 [role=dialog] 内，先点叶子 'Normal'，再点 btn_text。"""
    return (
        "(function(){var dlg=null;var dialogs=document.querySelectorAll('[role=dialog]');"
        "for(var i=0;i<dialogs.length;i++){"
        "if(dialogs[i].textContent.indexOf('" + _js_str(title_contains) + "')>=0){dlg=dialogs[i];break;}}"
        "if(!dlg)return 'nodialog';"
        "var a=dlg.querySelectorAll('*');"
        "for(var i=0;i<a.length;i++){"
        "if(a[i].textContent.trim()==='Normal'&&a[i].children.length===0){a[i].click();break;}}"
        "var b=dlg.querySelectorAll('button');"
        "for(var i=0;i<b.length;i++){"
        "if(b[i].textContent.trim()==='" + _js_str(btn_text) + "'){b[i].click();return 'downloaded';}}"
        "return 'nobtn';})();"
    )


def js_dialog_click(btn_text: str) -> str:
    """任意 [role=dialog] 内点 btn_text（步骤 6 标签下载弹窗）。"""
    return (
        "(function(){var dlg=document.querySelector('[role=dialog]');"
        "if(!dlg)return 'nodialog';"
        "var b=dlg.querySelectorAll('button');"
        "for(var i=0;i<b.length;i++){"
        "if(b[i].textContent.trim()==='" + _js_str(btn_text) + "'){b[i].click();return 'downloaded';}}"
        "return 'nobtn';})();"
    )


# --- 步骤 7：箱数（execCommand insertText 已验证）+ pallets checkbox + fragile ---
def js_fill_box_qty(box: str, selector: str) -> str:
    return (
        "(function(){var q=document.querySelector('" + selector + "');"
        "if(!q)return 'notfound';"
        "q.click();q.focus();q.select();"
        "document.execCommand('insertText', false, '" + _js_str(box) + "');"
        "q.dispatchEvent(new Event('change',{bubbles:true}));"
        "return 'filled';})();"
    )


JS_CHECK_PALLETS = """(function(){
  var labs = document.querySelectorAll('label.andes-checkbox');
  if (labs.length < 3) return 'need3:' + labs.length;
  for (var i = 1; i < 3; i++) {
    var lab = labs[i];
    var r = lab.getBoundingClientRect(), x = r.left + r.width/2, y = r.top + r.height/2;
    lab.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1}));
    lab.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:0}));
    lab.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,view:window}));
  }
  return 'checked';
})();"""

JS_CHECK_FRAGILE = """(function(){
  var el = document.querySelector('[data-testid="checkbox-fragils-consolidation"]');
  if (!el) return 'notfound';
  el.scrollIntoView({block: 'center'});
  var input = el.tagName === 'INPUT' ? el : el.querySelector('input[type=checkbox]');
  if (input) {
    input.checked = true;
    input.dispatchEvent(new Event('change', {bubbles: true}));
    input.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
  }
  var c = (el.closest && el.closest('[data-andes-checkbox-container]')) || el;
  var k = Object.keys(c).find(function(x){ return x.startsWith('__reactFiber'); });
  if (k) {
    var n = c[k];
    for (var j = 0; j < 15 && n; j++) {
      if (n.memoizedProps && n.memoizedProps.onChange) {
        n.memoizedProps.onChange({target:{checked:true},preventDefault:function(){},stopPropagation:function(){}});
        break;
      }
      n = n.return;
    }
  }
  return 'checked';
})();"""

# --- 步骤 8：取消预约 ---
JS_CLICK_2ND_EDITAR = """(function(){
  var l = document.querySelectorAll('a');
  var c = 0;
  for (var i = 0; i < l.length; i++) {
    if (l[i].textContent.trim() === 'Editar') {
      c++;
      if (c === 2) { l[i].click(); return 'clicked'; }
    }
  }
  return 'notfound';
})();"""

JS_SCROLL_AND_CLICK_RESERVA = """(function(){
  window.scrollTo(0, 1500);
  var b = document.querySelectorAll('button');
  for (var i = 0; i < b.length; i++) {
    if (b[i].textContent.trim() === 'Cancelar reserva') { b[i].click(); return 'clicked'; }
  }
  return 'notfound';
})();"""

JS_CLICK_CANCELAR_CITA = """(function(){
  var b = document.querySelectorAll('button');
  for (var i = 0; i < b.length; i++) {
    if (b[i].textContent.trim() === 'Cancelar cita') { b[i].click(); return 'clicked'; }
  }
  return 'notfound';
})();"""


# ────────────────────────────────────────────────
# 编排器
# ────────────────────────────────────────────────

@dataclass
class RunState:
    """单次运行累积状态。"""
    record_id: str = ""
    sku: str = ""
    name: str = ""
    qty: str = ""
    box: str = ""
    shipment_id: str = ""
    ml_code: str = "UNKNOWN"
    completed_steps: list[int] = field(default_factory=list)
    files_uploaded: dict[str, str] = field(default_factory=dict)
    dry_run_notes: list[str] = field(default_factory=list)


class Orchestrator:
    """8 步 FULL 货件流程编排器。"""

    def __init__(self, cfg: Config, selectors: Selectors) -> None:
        self.cfg = cfg
        self.sel = selectors
        self.ziniao = ZiniaoClient(cfg, selectors)
        self.feishu = FeishuClient(cfg)
        self.state = RunState()

    # ---- 日志（stderr，保持 stdout 纯 JSON）----
    @staticmethod
    def _log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)

    # ---- 步骤执行骨架 ----
    def _guard_write(self, step: int, allow_write: bool, dry_run: bool) -> bool:
        """写步骤保护：dry_run 跳过；未批准（allow_write=False）则报告 needs_approval。"""
        if dry_run:
            self._log(f"  步骤{step} ({STEP_NAMES[step]}) 为写操作，dry_run 跳过")
            self.state.dry_run_notes.append(f"步骤{step} 写操作已跳过（dry_run）")
            return False
        if not allow_write:
            raise StepError(step, "needs_approval",
                            f"步骤{step} ({STEP_NAMES[step]}) 是写操作，需 Agent 确认后以 "
                            f"--allow-write 重新执行")
        return True

    def _mark_done(self, step: int) -> None:
        if step not in self.state.completed_steps:
            self.state.completed_steps.append(step)
        self._log(f"  ✅ 步骤{step} ({STEP_NAMES[step]}) 完成")

    # ==================================================
    # 步骤 1：前期准备（只读）
    # ==================================================
    def step1_prepare(self) -> dict[str, Any]:
        self._log("步骤1 前期准备：打开店铺并检查 FULL 管理页")
        self.ziniao.open_store(step=1)
        self.ziniao.visit(INBOUNDS_URL, step=1, wait_after=2.0)
        out = self.ziniao.exec_js(JS_EXTRACT_SHIPMENTS, step=1)
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = {"shipments": [], "capacity_warning": None}
        shipments: list[dict[str, Any]] = data.get("shipments", [])
        expired = [s for s in shipments if s.get("status") == "Vencido"]
        in_prep = [s for s in shipments if s.get("status") == "En preparación"]
        warning = data.get("capacity_warning")
        summary = {
            "shipment_count": len(shipments),
            "expired": [s["id"] for s in expired],
            "en_preparacion": len(in_prep),
            "capacity_warning": bool(warning),
        }
        self._log(f"  货件数={len(shipments)} 过期={len(expired)} "
                  f"En preparación={len(in_prep)} 库容警告={bool(warning)}")
        self._mark_done(1)
        return summary

    # ==================================================
    # 步骤 2：点击 Enviar productos 进入创建入口（只读导航）
    # ==================================================
    def step2_entry(self) -> None:
        self._log("步骤2 货件创建入口：点击 Enviar productos")
        self.ziniao.visit(INBOUNDS_URL, step=2, wait_after=2.0)
        result = self.ziniao.exec_with_fallback(
            2, "enviar_btn",
            lambda sel: js_click_button("Enviar productos", sel),
            expected="clicked", retries=3)
        self._log(f"  Enviar productos 点击成功 ({result})")
        # 等待导航到 Planificación 页面（轮询搜索框出现）
        sku_chain = self.sel.chain(3, "sku_input")
        self.ziniao.wait_for(
            lambda sel: ("(function(){return document.querySelector('" + sel +
                         "')?'ready':'waiting';})();"),
            sku_chain[0], "ready", timeout_s=24, interval_s=2.0, step=2,
            action="planificacion_page")
        self._mark_done(2)

    # ==================================================
    # 步骤 3：选择产品与数量（写操作）
    # ==================================================
    def step3_select_product(self) -> None:
        self._log("步骤3 选择产品与数量")
        sku_chain = self.sel.chain(3, "sku_input")
        # 3a. 搜索 SKU
        self.ziniao.exec_with_fallback(
            3, "sku_input", lambda sel: js_search_sku(self.state.sku, sel),
            expected="searched", retries=3, wait_after=3.0)
        self._log(f"  SKU {self.state.sku} 已搜索")
        # 提取 ML 码（非关键，失败 UNKNOWN 兜底）
        try:
            self.state.ml_code = self.ziniao.exec_js(JS_EXTRACT_ML_CODE, step=3, retries=1)
        except StepError:
            self.state.ml_code = "UNKNOWN"
        self._log(f"  ML码: {self.state.ml_code}")
        # 3b. 填写数量
        self.ziniao.exec_with_fallback(
            3, "qty_input", lambda sel: js_fill_qty(self.state.qty, sel),
            expected="filled", retries=3, wait_after=1.0)
        # 等待按钮变为 enabled（填数量后约 3 秒）
        self._log("  等待 Continuar 按钮启用...")
        time.sleep(3.0)
        # 3c. Continuar
        self.ziniao.exec_with_fallback(
            3, "continuar_btn", lambda sel: js_click_button("Continuar", sel),
            expected="clicked", retries=3, wait_after=2.0)
        # 3d. 弹窗 Continuar con mi plan actual（轮询 10×2s）
        modal_chain = self.sel.chain(3, "plan_modal_btn")
        self.ziniao.wait_for(
            lambda sel: ("(function(){var b=document.querySelectorAll('" + sel + "');"
                         "for(var i=0;i<b.length;i++){"
                         "if(b[i].textContent.trim()==='Continuar con mi plan actual')"
                         "return 'found';}return 'waiting';})();"),
            modal_chain[0], "found", timeout_s=20, interval_s=2.0, step=3,
            action="plan_modal")
        self.ziniao.exec_with_fallback(
            3, "plan_modal_btn", lambda sel: js_click_button("Continuar con mi plan actual", sel),
            expected="clicked", retries=3, wait_after=3.0)
        # 3e. 货件号（URL /plans/(\d+)/，兜底 8 位数字）
        shipment = self.ziniao.exec_js(JS_EXTRACT_SHIPMENT_ID, step=3)
        if shipment == "UNKNOWN" or not shipment:
            raise StepError(3, "business", "未从 URL 提取到货件号", recovery_attempted=["url_regex"])
        self.state.shipment_id = shipment
        self._log(f"  货件号: {shipment}")
        self.feishu.update_field(self.state.record_id, "货件号", shipment)
        self._mark_done(3)
        if self.state.record_id:
            self.feishu.send_message(
                f"✅ 步骤1-3完成: 货件 #{shipment} {self.state.sku} {self.state.qty}件")

    # ==================================================
    # 步骤 4：货件预约时间（写操作）
    # ==================================================
    def step4_appointment(self) -> None:
        self._log("步骤4 货件预约时间")
        # 4a. 进入预约页（优先从URL提取inbound ID，其次用货件号拼plans路径）
        self.ziniao.visit_plan_page("appointment-v2", self.state.shipment_id, step=4)
        time.sleep(3)
        # 等待预约页加载（轮询运输方式下拉框）
        dd_chain = self.sel.chain(4, "shipment_dropdown")
        self.ziniao.wait_for(
            lambda sel: ("(function(){return document.querySelector('" + sel +
                         "')?'ready':'waiting';})();"),
            dd_chain[0], "ready", timeout_s=20, interval_s=2.0, step=4,
            action="appointment_page")
        # 4b. 运输方式下拉（分两步：PointerEvent开下拉 → 选Vehículo，仿照旧bash验证过的两阶段调用）
        # 阶段1：PointerEvent 全序列打开 Andes combobox（用精确ID，匹配旧bash选择器）
        dropdown_js = (
            "(function(){"
            "var c=document.getElementById('shipment-type-selection-dropdown-id-trigger');"
            "if(!c){var cb=document.querySelector('[role=combobox]');if(cb)c=cb;}"
            "if(!c)return 'notfound';"
            "var r=c.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;"
            "c.dispatchEvent(new PointerEvent('pointerover',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse'}));"
            "c.dispatchEvent(new PointerEvent('pointerenter',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse'}));"
            "c.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1}));"
            "c.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:0}));"
            "c.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,view:window}));"
            "return 'opened';})();"
        )
        self.ziniao.exec_js(dropdown_js, step=4, wait_after=2.0)
        # 阶段2：选 Vehículo particular（下拉选项在第二次调用中可访问）
        vehicle_js = (
            "(function(){var o=document.querySelectorAll('[role=option]');"
            "for(var i=0;i<o.length;i++){"
            "if(o[i].textContent.indexOf('Vehículo')>=0){o[i].click();return 'selected';}}"
            "return 'notfound';})();"
        )
        r = self.ziniao.exec_js(vehicle_js, step=4)
        if r != "selected":
            raise StepError(4, "selector_not_found", "未找到 Vehículo 运输选项",
                            recovery_attempted=["alt_selector:vehicle_option"])
        self._log("  配送方式: Vehículo particular")
        # 4c. 日期选择：灰圈算法（div.day--current 之后第 30 格，跳过表头）
        date_chain = self.sel.chain(4, "date_input")
        self.ziniao.exec_with_fallback(
            4, "date_input", lambda sel: js_pointer_click(sel),
            expected="clicked", retries=3)
        time.sleep(1)
        picked = self._pick_date()
        self._log(f"  预约日期: {picked}")
        # 4d. 时间选择（第一个 div.hour）
        hour_chain = self.sel.chain(4, "hour")
        r = self.ziniao.exec_js(
            "(function(){var h=document.querySelector('" + hour_chain[0] + "');"
            "if(!h)return 'notfound';"
            "var el=h;var rr=el.getBoundingClientRect(),x=rr.left+rr.width/2,y=rr.top+rr.height/2;"
            "el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1}));"
            "el.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:0}));"
            "el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,view:window}));"
            "return 't='+(el.textContent||'').trim();})();",
            step=4, wait_after=1.0)
        if not r.startswith("t="):
            raise StepError(4, "selector_not_found", "时间选择失败",
                            recovery_attempted=["alt_selector:hour"])
        self._log(f"  预约时间: {r[2:]}")
        # 4e. 确认（分两次：第1次点日历区 Confirmar，第2次点主确认）
        confirm_chain = self.sel.chain(4, "confirm_btn")
        r = self.ziniao.exec_js(js_click_button("Confirmar", confirm_chain[0],
                                                require_enabled=False),
                                step=4)
        if r != "clicked":
            raise StepError(4, "selector_not_found", "未找到第1个 Confirmar（日历区）",
                            recovery_attempted=["retry_3x"])
        time.sleep(2)
        r = self.ziniao.exec_js(js_click_button("Confirmar", confirm_chain[0],
                                                require_enabled=True),
                                step=4)
        if r != "clicked":
            raise StepError(4, "selector_not_found", "主 Confirmar 不可用或未找到",
                            recovery_attempted=["retry_3x"])
        time.sleep(5)
        self._mark_done(4)
        if self.state.record_id:
            self.feishu.send_message(f"✅ 步骤4完成: #{self.state.shipment_id} 已预约")

    def _pick_date(self) -> str:
        """灰圈算法：找 div.day--current，从其后的第 30 格选日期（跳过表头）。

        与 poll-fulfillment.sh 验证过的算法一致；若当前视图不足则翻月重试一次。
        """
        day_chain = self.sel.chain(4, "day")
        day_sel = day_chain[0]
        current_sel = "div.day--current"
        for flip in range(2):
            script = (
                "(function(){var cur=document.querySelector('" + current_sel + "');"
                "if(!cur)return 'notfound';"
                "var days=Array.from(document.querySelectorAll('" + day_sel + "'));"
                "var idx=days.indexOf(cur)+30;"
                "if(idx<0||idx>=days.length)return 'need_flip';"
                "while(idx<days.length&&/^[A-Z]+$/.test((days[idx].textContent||'').trim()))idx++;"
                "var d=days[idx];"
                "if(!d)return 'notfound';"
                "var r=d.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;"
                "d.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1}));"
                "d.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:0}));"
                "d.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,view:window}));"
                "return 'd='+(d.textContent||'').trim();})();"
            )
            r = self.ziniao.exec_js(script, step=4, wait_after=1.0)
            if r.startswith("d="):
                return r[2:]
            if r == "need_flip" and flip == 0:
                # 翻月：fiber onClick(next month)，下一调用验证月已变
                nm_chain = self.sel.chain(4, "next_month")
                r2 = self.ziniao.exec_js(
                    "(function(){var n=document.querySelector('" + nm_chain[0] + "');"
                    "if(!n)return 'notfound';"
                    "var k=Object.keys(n).find(function(x){return x.startsWith('__reactFiber');});"
                    "var node=k?n[k]:null;"
                    "for(var j=0;j<15&&node;j++){"
                    "if(node.memoizedProps&&node.memoizedProps.onClick){"
                    "node.memoizedProps.onClick({});break;}node=node.return;}"
                    "n.click();return 'flipped';})();",
                    step=4, retries=2, wait_after=1.5)
                if r2 != "flipped":
                    raise StepError(4, "selector_not_found", "翻月按钮不可用",
                                    recovery_attempted=["fiber_onClick", "next_month"])
                continue
            raise StepError(4, "selector_not_found", f"日期选择失败: {r}",
                            recovery_attempted=[f"gray_circle_flip_{flip}"])
        raise StepError(4, "selector_not_found", "日期选择失败：翻月后仍无法选中",
                        recovery_attempted=["gray_circle_flip_2"])

    # ==================================================
    # 步骤 5：包装确认（写操作）
    # ==================================================
    def step5_package_confirm(self) -> None:
        self._log("步骤5 包装确认")
        self.ziniao.visit_plan_page("procedures", self.state.shipment_id, step=5)
        time.sleep(3)
        r = self.ziniao.exec_js(JS_PACKAGE_CONFIRM, step=5)
        if r != "done":
            raise StepError(5, "business", f"包装确认失败 ({r})",
                            recovery_attempted=["checkbox_mouseevent", "confirm_click"])
        time.sleep(3)
        self._mark_done(5)

    # ==================================================
    # 步骤 6：标签下载（只读下载）
    # ==================================================
    def step6_labels(self, dry_run: bool = False) -> None:
        self._log("步骤6 标签下载")
        # dry_run 且无货件号：没有可下载标签的货件，直接记为跳过（避免访问不存在的页面）
        if dry_run and not self.state.shipment_id:
            self.state.dry_run_notes.append(
                "步骤6 跳过：dry_run 无货件号（货件由写步骤 3 创建）")
            self._log("  dry_run 无货件号，步骤6 跳过")
            return
        self.ziniao.visit_plan_page("labeling", self.state.shipment_id, step=6)
        time.sleep(3)
        # 6-1. 勾选所有 checkbox（combined 方案：checked + change + MouseEvent + fiber onChange）
        r = self.ziniao.exec_js(JS_LABEL_CHECKBOXES, step=6, wait_after=2.0)
        if not r.startswith("checked:"):
            raise StepError(6, "selector_not_found", f"勾选产品 checkbox 失败 ({r})",
                            recovery_attempted=["fiber_onChange", "mouse_event"])
        self._log(f"  复选框: {r}")
        if dry_run:
            self.state.dry_run_notes.append("步骤6 只做勾选探测（dry_run 不触发下载）")
            self._mark_done(6)
            return
        # 6-2. Descargar etiquetas（等 React 渲染后按钮启用）
        self.ziniao.exec_with_fallback(
            6, "descargar_btn",
            lambda sel: js_click_button("Descargar etiquetas", sel),
            expected="clicked", retries=3, wait_after=2.0)
        # 6-3. 弹窗「¿Cómo quieres descargar tus etiquetas?」→ 点「Descargar」
        r = self.ziniao.exec_js(js_dialog_click("Descargar"), step=6, wait_after=3.0)
        if r != "downloaded":
            raise StepError(6, "selector_not_found", f"弹窗 Descargar 未找到 ({r})",
                            recovery_attempted=["role_dialog"])
        # 6-4. Confirmar 完成标签步骤
        self.ziniao.exec_with_fallback(
            6, "confirm_btn", lambda sel: js_click_button("Confirmar", sel),
            expected="clicked", retries=3, wait_after=3.0)
        # 上传产品标签（自动重命名 + 飞书 Base 附件）
        self._upload_latest_pdf(
            "Etiquetas-de-producto-*.pdf",
            f"产品标-{self.state.sku}-{self.state.ml_code}-{self._safe_name(self.state.name)}.pdf",
            "产品标签")
        self._mark_done(6)
        if self.state.record_id:
            self.feishu.send_message("✅ 步骤6完成: 产品标签已上传")

    # ==================================================
    # 步骤 7：打印箱唛（写操作）
    # ==================================================
    def step7_box_labels(self) -> None:
        self._log("步骤7 打印箱唛")
        self.ziniao.visit_plan_page("volumes", self.state.shipment_id, step=7)
        time.sleep(3)
        # 7a. 填箱数（focus + select + execCommand insertText）
        self.ziniao.exec_with_fallback(
            7, "qty_input", lambda sel: js_fill_box_qty(self.state.box, sel),
            expected="filled", retries=3, wait_after=1.0)
        # 7b. 只勾选 Andes checkbox #2/#3（Pallets 选项），跳过 #1（bultos）
        r = self.ziniao.exec_js(JS_CHECK_PALLETS, step=7, wait_after=1.0)
        if r != "checked":
            raise StepError(7, "selector_not_found", f"Andes checkbox 不足 3 个 ({r})",
                            recovery_attempted=["pointer_event"])
        # 7c. Generar etiquetas
        self.ziniao.exec_with_fallback(
            7, "generate_btn", lambda sel: js_click_button("Generar etiquetas", sel),
            expected="clicked", retries=3, wait_after=5.0)
        # 7d. Descarga todas las etiquetas
        self.ziniao.exec_with_fallback(
            7, "download_all", lambda sel: js_click_button_contains("Descarga todas", sel),
            expected="clicked", retries=3, wait_after=2.0)
        # 7e. 弹窗「Descarga e imprime las etiquetas」→ Normal → Descargar etiquetas
        r = self.ziniao.exec_js(js_modal_click("Descarga e imprime", "Descargar etiquetas"),
                                step=7, wait_after=3.0)
        if r != "downloaded":
            raise StepError(7, "selector_not_found", f"箱唛弹窗下载失败 ({r})",
                            recovery_attempted=["role_dialog", "leaf_normal"])
        # 7f. 勾选 fragile checkbox（combined approach）
        r = self.ziniao.exec_js(JS_CHECK_FRAGILE, step=7, wait_after=1.0)
        if r != "checked":
            raise StepError(7, "selector_not_found", f"未找到 fragile checkbox ({r})",
                            recovery_attempted=["fiber_onChange"])
        # 7g. Continuar
        self.ziniao.exec_with_fallback(
            7, "continuar_btn", lambda sel: js_click_button("Continuar", sel),
            expected="clicked", retries=3, wait_after=3.0)
        # 上传箱唛（自动重命名 + 飞书 Base 附件）
        self._upload_latest_pdf(
            "Envio-*-Etiquetas-de-bultos.pdf",
            f"{self.state.shipment_id}-{self.state.box}箱.pdf",
            "箱唛")
        self._mark_done(7)
        if self.state.record_id:
            self.feishu.send_message("✅ 步骤7完成: 箱唛已上传")

    # ==================================================
    # 步骤 8：取消预约（写操作）
    # ==================================================
    def step8_cancel_appointment(self) -> None:
        self._log("步骤8 取消预约")
        self.ziniao.visit(INBOUNDS_URL, step=8, wait_after=3.0)
        # 第 2 个 Editar 链接
        r = self.ziniao.exec_js(JS_CLICK_2ND_EDITAR, step=8)
        if r != "clicked":
            raise StepError(8, "selector_not_found", "未找到第2个 Editar 链接",
                            recovery_attempted=["nth_link"])
        time.sleep(3)
        r = self.ziniao.exec_js(JS_SCROLL_AND_CLICK_RESERVA, step=8)
        if r != "clicked":
            raise StepError(8, "selector_not_found", "未找到 Cancelar reserva 按钮",
                            recovery_attempted=["scroll_1500"])
        time.sleep(1)
        r = self.ziniao.exec_js(JS_CLICK_CANCELAR_CITA, step=8)
        if r != "clicked":
            raise StepError(8, "selector_not_found", "未找到 Cancelar cita 按钮",
                            recovery_attempted=["retry_3x"])
        time.sleep(2)
        self._mark_done(8)

    # ==================================================
    # 文件处理：自动重命名 + 上传飞书 Base
    # ==================================================
    @staticmethod
    def _safe_name(name: str) -> str:
        return re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip() or "产品"

    def _upload_latest_pdf(self, pattern: str, rename_to: str, field_name: str) -> None:
        """从下载目录找最新匹配的 PDF，重命名后上传到飞书 Base 附件字段。"""
        dl_dir = Path(self.cfg.ziniaodl)
        if not dl_dir.exists():
            self._log(f"  ⚠️ 下载目录不存在: {dl_dir}")
            return
        candidates = sorted(dl_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            self._log(f"  ⚠️ 未找到 {pattern}，跳过上传")
            return
        src = candidates[0]
        renamed = dl_dir / rename_to
        try:
            src.replace(renamed)
        except OSError as exc:
            self._log(f"  ⚠️ 重命名失败: {exc}")
            renamed = src
        if self.state.record_id and self.cfg.feishu_ready:
            self.feishu.upload_attachment(self.state.record_id, field_name, str(renamed))
            self._log(f"  📎 已上传 {field_name}: {renamed.name}")
            self.state.files_uploaded[field_name] = renamed.name
        else:
            self._log(f"  文件已重命名（未上传）: {renamed.name}")

    # ==================================================
    # 主流程
    # ==================================================
    def run(self, mode: str, allow_write: bool, step_filter: Optional[int],
            record_id: Optional[str], sku: Optional[str], qty: Optional[str],
            box: Optional[str], shipment_id: Optional[str]) -> dict[str, Any]:
        """执行编排，返回结构化结果 dict（异常也会被捕获转为 failed JSON）。"""
        self._log(f"🚀 FULL 货件编排启动 mode={mode} allow_write={allow_write}")

        # 记录解析：显式传入或查询第一条 Pending+就绪
        if record_id:
            self.state.record_id = record_id
            if sku:
                self.state.sku, self.state.qty = sku, qty or ""
                self.state.box = box or ""
        else:
            pending = self.feishu.list_pending()
            if not pending:
                self._log("没有待处理的 Pending 记录，本轮退出。")
                return {"status": "no_pending", "record_id": None, "shipment_id": None,
                        "sku": None, "completed_steps": [], "failed_step": None,
                        "error": None, "files_uploaded": {}, "dry_run": mode == "dry-run",
                        "write_steps": list(WRITE_STEPS)}
            rec = pending[0]
            self.state.record_id = rec["record_id"]
            self.state.sku = rec["sku"]
            self.state.name = rec["name"]
            self.state.qty = rec["qty"]
            self.state.box = rec["box"]
            # 若记录已有货件号（上次部分完成），跳过步骤 3 继续执行
            existing_shipment = rec.get("shipment_id", "")
            if existing_shipment and existing_shipment not in ("", "None", "null"):
                self.state.shipment_id = existing_shipment
                self._log(f"  已有货件号 {existing_shipment}，从步骤4继续")
        if shipment_id:
            self.state.shipment_id = shipment_id
        self._log(f"🚀 处理: {self.state.sku} {self.state.name} "
                  f"{self.state.qty}件 {self.state.box}箱 (record={self.state.record_id})")

        dry_run = mode == "dry-run"
        # dry_run 模式不写飞书 Base（保持零副作用）
        if self.state.record_id and not dry_run:
            self.feishu.update_field(self.state.record_id, "状态", "运行中")
            self.feishu.update_step(self.state.record_id, "步骤1：打开店铺")
        steps = [step_filter] if step_filter else list(range(1, 9))
        step_summaries: dict[int, Any] = {}

        for step in steps:
            try:
                # 写步骤保护（需在 try 内，needs_approval 要走统一结果通道）
                if step in WRITE_STEPS:
                    if not self._guard_write(step, allow_write, dry_run):
                        continue
                if step == 1:
                    step_summaries[1] = self.step1_prepare()
                elif step == 2:
                    self.step2_entry()
                elif step == 3:
                    if self.state.shipment_id:
                        self._log(f"  跳过步骤3（已有货件号 {self.state.shipment_id}）")
                        self._mark_done(3)
                    else:
                        self.step3_select_product()
                elif step == 4:
                    self.step4_appointment()
                elif step == 5:
                    self.step5_package_confirm()
                elif step == 6:
                    self.step6_labels(dry_run=dry_run)
                elif step == 7:
                    self.step7_box_labels()
                elif step == 8:
                    self.step8_cancel_appointment()
            except StepError as exc:
                if exc.err_type == "needs_approval":
                    return self._result("needs_approval", failed_step=step,
                                        error=exc.to_dict(), step_summaries=step_summaries,
                                        dry_run=dry_run)
                self._log(f"❌ 步骤{step} 失败: {exc.message}")
                if self.state.record_id:
                    self.feishu.update_step(self.state.record_id, f"失败：{exc.message}")
                    self.feishu.send_message(f"❌ FULL 货件失败: {exc.message}")
                status = "failed" if not self.state.completed_steps else "partial"
                return self._result(status, failed_step=step, error=exc.to_dict(),
                                    step_summaries=step_summaries, dry_run=dry_run)
            # 每步完成后同步 Base 当前步骤（dry_run 不写）
            if self.state.record_id and step < 8 and not dry_run:
                self.feishu.update_step(self.state.record_id, f"步骤{step + 1}：{STEP_NAMES[step + 1]}")

        # ── 全部完成 ──
        if self.state.record_id:
            self.feishu.update_field(self.state.record_id, "状态", "已完成")
            self.feishu.update_step(self.state.record_id, "全部完成")
            self.feishu.update_field(self.state.record_id, "就绪", False)
            self.feishu.send_message(
                f"🎉 FULL 货件完成！\nSKU: {self.state.sku} {self.state.name}\n"
                f"货件: #{self.state.shipment_id}\n数量: {self.state.qty}件 / {self.state.box}箱\n"
                f"文件已上传到飞书多维表格")
        self.ziniao.close_store()
        self._log(f"✅ {self.state.sku or '（无记录）'} 完成")
        return self._result("success", failed_step=None, error=None,
                            step_summaries=step_summaries, dry_run=dry_run)

    def _result(self, status: str, failed_step: Optional[int],
                error: Optional[dict[str, Any]], step_summaries: dict[int, Any],
                dry_run: bool) -> dict[str, Any]:
        """组装最终 JSON 结果。"""
        completed = sorted(self.state.completed_steps)
        skipped_write = sorted(s for s in WRITE_STEPS
                               if dry_run and s not in completed)
        result: dict[str, Any] = {
            "status": status,
            "record_id": self.state.record_id or None,
            "shipment_id": self.state.shipment_id or None,
            "sku": self.state.sku or None,
            "completed_steps": completed,
            "failed_step": failed_step,
            "error": error,
            "files_uploaded": self.state.files_uploaded,
            "dry_run": dry_run,
            "write_steps": list(WRITE_STEPS),
        }
        if dry_run:
            result["skipped_write_steps"] = skipped_write
            result["dry_run_notes"] = self.state.dry_run_notes
        if step_summaries:
            result["step_summaries"] = step_summaries
        if status == "needs_approval":
            result["next_step"] = failed_step
            result["next_step_name"] = STEP_NAMES.get(failed_step or 0, "")
        return result


# ────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fulfillment_orchestrator.py",
        description="FULL 货件编排器：结构化封装 ziniao-cli/lark-cli，返回 JSON 给 Agent。")
    p.add_argument("--mode", choices=["full", "dry-run", "step", "inspect"],
                   default="full", help="full=全流程; dry-run=只读步骤; step=单步; inspect=自检")
    p.add_argument("--step", type=int, choices=list(range(1, 9)),
                   help="step 模式下的步骤号")
    p.add_argument("--allow-write", action="store_true",
                   help="批准写步骤(3/4/5/7/8)执行（默认拒绝）")
    p.add_argument("--record-id", help="指定飞书记录（缺省自动查询第一条 Pending+就绪）")
    p.add_argument("--sku", help="SKU（配合 --record-id 使用）")
    p.add_argument("--qty", help="数量（配合 --record-id 使用）")
    p.add_argument("--box", help="箱数（配合 --record-id 使用）")
    p.add_argument("--shipment-id", help="货件号（步骤 6/7/8 恢复时使用）")
    p.add_argument("--store-id", help="覆盖店铺 ID")
    p.add_argument("--store-name", help="覆盖店铺名称")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    if args.store_id:
        cfg.store_id = args.store_id
    if args.store_name:
        cfg.store_name = args.store_name

    # inspect：无副作用自检
    if args.mode == "inspect":
        try:
            sel = Selectors()
        except StepError as exc:
            print(json.dumps({"status": "failed", "error": exc.to_dict()},
                             ensure_ascii=False))
            return 1
        result = {
            "status": "ok",
            "fulfillment_js": str(FULFILLMENT_JS),
            "selectors": sel.summary(),
            "config": {
                "feishu_ready": cfg.feishu_ready,
                "browser_ready": cfg.browser_ready,
                "env_file": str(_default_env_file()),
            },
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.mode == "step" and not args.step:
        print(json.dumps({"status": "failed", "error": {
            "step": None, "type": "business", "message": "--mode step 需要 --step N",
            "recovery_attempted": []}}, ensure_ascii=False))
        return 2

    try:
        sel = Selectors()
    except StepError as exc:
        print(json.dumps({"status": "failed", "error": exc.to_dict()}, ensure_ascii=False))
        return 1

    orch = Orchestrator(cfg, sel)
    try:
        result = orch.run(mode=args.mode, allow_write=args.allow_write,
                          step_filter=args.step, record_id=args.record_id,
                          sku=args.sku, qty=args.qty, box=args.box,
                          shipment_id=args.shipment_id)
    except StepError as exc:  # 顶层兜底（如配置缺失/步骤保护未捕获）
        result = {"status": "failed", "record_id": orch.state.record_id or None,
                  "shipment_id": orch.state.shipment_id or None,
                  "sku": orch.state.sku or None,
                  "completed_steps": orch.state.completed_steps,
                  "failed_step": exc.step or None,
                  "error": exc.to_dict(), "files_uploaded": orch.state.files_uploaded}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in ("success", "no_pending", "needs_approval") else 1


if __name__ == "__main__":
    sys.exit(main())
