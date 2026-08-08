#!/usr/bin/env python3
"""FULL 货件编排器 — Playwright CDP 驱动的结构化封装（替代 ziniao-cli / poll-fulfillment.sh）。

架构（与旧 ziniao-cli 的本原区别）:
    Cron 触发 wrapper (poll-fulfillment.sh, no_agent)
      → 按店铺并行 spawn /tmp/pw-venv/bin/python3 fulfillment_orchestrator.py \
        --mode full --allow-write --store-name <店名>
        → 入口先拿店铺级 flock（/tmp/ziniao-<storeId>.lock），拿不到输出 skipped 退出
        → PlaywrightClient: connect_over_cdp(127.0.0.1:52420) 直连紫鸟浏览器
        → FeishuClient: lark-cli 多维表格 + 消息
        → 每步有: 选择器 fallback 链、自动等待、失败自动截图
        → 返回结构化 JSON

用法:
    /tmp/pw-venv/bin/python3 fulfillment_orchestrator.py --mode inspect
        自检: 加载 fulfillment.js SELECTORS、检查配置完整性（无副作用）
    /tmp/pw-venv/bin/python3 fulfillment_orchestrator.py --mode dry-run [--record-id recXXX]
        只执行只读步骤 1/2/6，写步骤(3/4/5/7/8)全部跳过
    /tmp/pw-venv/bin/python3 fulfillment_orchestrator.py --mode full [--allow-write]
        全流程编排。写步骤默认拒绝，需 --allow-write 才执行；--store-name <店名> 仅处理该店铺记录
    /tmp/pw-venv/bin/python3 fulfillment_orchestrator.py --mode step --step N [--allow-write]
        从指定步骤继续（Agent 确认后恢复执行）

返回: stdout 单行 JSON（progress 日志走 stderr，保证 stdout 纯净）
    {
      "status": "success|partial|failed|no_pending|needs_approval|skipped",
      "record_id": "recXXXX", "shipment_id": "12345678", "sku": "HW-MX-026-01",
      "completed_steps": [1,2,3], "failed_step": null,
      "error": {"step":4,"type":"selector_not_found","message":"...","recovery_attempted":[...]},
      "files_uploaded": {"产品标签":"xxx.pdf","箱唛":"yyy.pdf"}
    }

凭据: 一律来自环境变量（FEISHU_BASE_TOKEN / FEISHU_TABLE_ID / ML_STORE_ID /
ML_STORE_NAME / FEISHU_USER_ID / ZINIAO_DL / ML_CDP_URL），或本地
~/.hermes/scripts/fulfillment.env。本文件不包含任何硬编码凭据。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
FULFILLMENT_JS = SCRIPT_DIR / "fulfillment.js"

ML_BASE_URL = "https://myaccount.mercadolibre.com.mx"
INBOUNDS_URL = f"{ML_BASE_URL}/shipping/inbounds"
DEFAULT_CDP_URL = "http://127.0.0.1:52420"

# Telemetry：失败诊断包云上报（opt-in）。端点对应 whyshu-svr /api/argent/telemetry/failure。
TELEMETRY_PATH = "/api/argent/telemetry/failure"
TELEMETRY_URL = os.environ.get("WHYSHU_API_URL", "https://whyshu.com") + TELEMETRY_PATH
VERSION = "2.4.1"  # 与 skills/mercadolibre/ml-fulfillment-sop/SKILL.md frontmatter 同步（运行时优先读 frontmatter）
LOG_TAIL_MAX_LINES = 200          # log_tail 最多 200 行
SCREENSHOT_B64_MAX_CHARS = 1024 * 1024  # 截图 base64 超过 1MB 不上传

# 本地失败诊断索引（纯本地，不上报；多店铺并行时单行 append 原子安全）
FAILURES_DIR = Path.home() / ".hermes" / "fulfillment-logs"
FAILURES_JSONL = FAILURES_DIR / "failures.jsonl"


def _append_failure(record: dict[str, Any]) -> None:
    """追加一条失败诊断记录到 failures.jsonl（JSONL：每行一个 JSON 对象）。

    单行 append（O_APPEND）多进程并发原子安全；写失败静默，不影响主流程。
    """
    try:
        FAILURES_DIR.mkdir(parents=True, exist_ok=True)
        with open(FAILURES_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 紫鸟「网络修复」页检测（层1 navigate 自愈 + 层2 网络类错误分类共用）
ZINIA_ERROR_URL_MARK = "chrome-extension://"
ZINIA_ERROR_PATH_MARK = "/error.html"
ZINIA_ERROR_KEYWORDS = ("正在修复", "网络波动", "请求平台超时")
ZINIA_NETWORK_RETRY_TAG = "zinia_network_retry"   # navigate 自愈重试的 recovery 标记
AUTO_RETRY_MAX = 2                                 # 网络类错误自动重置次数上限
SEARCH_WRONG_PAGE_MARKERS = ("Algunas de tus publicaciones",
                             "Tus publicaciones")   # Mis publicaciones 商品列表页特征


def _ziniao_error_by_diag(page_url: str, dom_summary: str) -> bool:
    """按失败现场（当前页 URL + DOM 摘要）判断是否紫鸟「网络修复」页。

    与 PlaywrightClient._is_ziniao_error_page() 的检测口径一致，供层2失败分支
    复用（此时浏览器可能已不可用，只能靠已抓取的现场数据判断）。
    """
    if ZINIA_ERROR_URL_MARK in page_url and ZINIA_ERROR_PATH_MARK in page_url:
        return True
    return any(k in (dom_summary or "") for k in ZINIA_ERROR_KEYWORDS)


# ────────────────────────────────────────────────
# Telemetry 配置读取（opt-in / auth_token / version / log_tail / 截图）
# ────────────────────────────────────────────────

# v0.4.0 主路径：配置统一在 ~/.hermes/profiles/argent/（与 argent CLI 的 PROFILE_HOME 一致）
PROFILE_HOME = Path(os.environ.get(
    "ARGENT_PROFILE_HOME", Path.home() / ".hermes" / "profiles" / "argent"
))


def _read_telemetry_opt_in() -> bool:
    """读取 telemetry_opt_in（正则解析嵌套键缩进格式，避免 PyYAML 依赖）。

    只读 v0.4.0 主路径 PROFILE_HOME/config.yaml 的 argent.telemetry_opt_in
    （如 "  telemetry_opt_in: true"）；显式 true/1/yes 即开启，其余关闭。
    """
    try:
        text = (PROFILE_HOME / "config.yaml").read_text(encoding="utf-8")
    except Exception:
        return False
    m = re.search(r"^\s*telemetry_opt_in\s*:\s*(\S+)\s*$", text, re.M | re.I)
    if m:
        return m.group(1).strip().lower() in ("true", "1", "yes")
    return False


def _read_auth_token() -> str:
    """读取问述账号 JWT：与 argent CLI 一致（PROFILE_HOME/.env 的 WHYSHU_API_KEY）。"""
    try:
        for line in (PROFILE_HOME / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("WHYSHU_API_KEY="):
                token = line.split("=", 1)[1].strip()
                if token:
                    return token
    except Exception:
        pass
    return ""


def _read_skill_version() -> str:
    """从 ml-fulfillment-sop SKILL.md frontmatter 读 version（沿目录向上探测，双副本通用），失败回退 VERSION。"""
    root = Path(__file__).resolve().parent
    while root != root.parent:
        cand = root / "skills" / "mercadolibre" / "ml-fulfillment-sop" / "SKILL.md"
        if cand.is_file():
            try:
                m = re.search(r"^version\s*:\s*([\w.\-]+)", cand.read_text(encoding="utf-8"), re.M)
                if m:
                    return m.group(1)
            except Exception:
                pass
        root = root.parent
    return VERSION


def _read_log_tail(log_file: str) -> str:
    """日志尾部最多 LOG_TAIL_MAX_LINES 行（本次运行日志；cron 经 FULFILLMENT_LOG_FILE 注入）。"""
    if not log_file:
        return ""
    try:
        lines = Path(log_file).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-LOG_TAIL_MAX_LINES:])
    except Exception:
        return ""


def _screenshot_base64(path: Optional[str]) -> str:
    """失败截图 base64（编码后 >1MB 不上传；读取失败返回空串）。"""
    if not path:
        return ""
    try:
        b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        return b64 if len(b64) <= SCREENSHOT_B64_MAX_CHARS else ""
    except Exception:
        return ""


def _discover_cdp_ports() -> list[int]:
    """从 ziniaobro 进程发现所有 CDP 调试端口。"""
    try:
        out = subprocess.run(
            ["lsof", "-i", "-P", "-n"],
            capture_output=True, text=True, timeout=10,
        )
        ports = []
        for line in out.stdout.splitlines():
            if "ziniaobro" in line and "LISTEN" in line:
                m = re.search(r":(\d+)\s", line)
                if m:
                    ports.append(int(m[1]))
        return ports
    except Exception:
        return []


def _discover_cdp_port() -> Optional[int]:
    """从 ziniaobro 进程自动发现 CDP 调试端口（每次重启紫鸟端口随机变化）。
    
    多店铺场景使用 _discover_cdp_ports() + _identify_port()。
    """
    try:
        out = subprocess.run(
            ["lsof", "-i", "-P", "-n"],
            capture_output=True, text=True, timeout=10,
        )
        for line in out.stdout.splitlines():
            if "ziniaobro" in line and "LISTEN" in line:
                m = re.search(r":(\d+)\s", line)
                if m:
                    return int(m[1])
    except Exception:
        pass
    return None

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

# 店铺映射（开发测试用，客户环境由 stores.json 提供）
# orchestrator 优先读本地 stores.json（~/.hermes/scripts/stores.json，初始化向导生成，
# key = 紫鸟环境名 = 飞书「店铺名称」单选选项）；stores.json 缺失时才回退内置 STORE_MAP
# （本机开发测试兜底，运行时日志标注 dev 模式）。内置映射不打包给客户。
STORE_MAP = {
    "1店": {"name": "1店-子账号", "store_id": "27477945046190"},
    "2店": {"name": "2店-子账号", "store_id": "27494792824433"},
    "3店": {"name": "3店-主账号", "store_id": "27581021073442"},
}

STORES_JSON = Path.home() / ".hermes" / "scripts" / "stores.json"


def _load_stores() -> dict[str, dict[str, Any]]:
    """读取本地店铺映射 stores.json（客户机器由初始化向导生成，不在 git 仓库）。

    key = 紫鸟环境名（ziniao-cli store list 的 storeName）= 飞书「店铺名称」单选选项；
    value = {"store_id", "platform", "cdp_port"}。
    文件缺失/损坏 → 返回 {}（调用方决定报错提示初始化向导或回退 STORE_MAP dev 模式）。
    """
    try:
        data = json.loads(STORES_JSON.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        pass
    return {}


def _resolve_store(store_name: str) -> tuple[Optional[dict[str, Any]], bool]:
    """按店铺名解析店铺信息 → (info, dev_mode)。

    优先本地 stores.json（环境名 key）；stores.json 缺失时回退内置 STORE_MAP
    （开发测试用，客户环境由 stores.json 提供）。
    """
    stores = _load_stores()
    if stores:
        return stores.get(store_name), False
    info = STORE_MAP.get(store_name)
    if not info:
        # STORE_MAP 兼容环境名直查（key=短名 1店 / name=环境名 1店-子账号）
        info = next((v for v in STORE_MAP.values() if v.get("name") == store_name), None)
    return info, True


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
    cdp_url: str = DEFAULT_CDP_URL

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
        cdp_url=pick("ML_CDP_URL") or (lambda p: f"http://127.0.0.1:{p}" if p else DEFAULT_CDP_URL)(_discover_cdp_port()),
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
        self.err_type = err_type          # selector_not_found | timeout | cli_error | parse_error | business | config_error
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
        "qty_input": (None, ['input.andes-form-control__field[type="text"]:not([placeholder])',
                             'input[class*="andes-form-control"]:not([placeholder])',
                             'input[id^="_r_"], input[id^="_R_"]',
                             'input[type="number"]']),
        "continuar_btn": (None, ["button"]),          # textContent === "Continuar" && !disabled
        "plan_modal_btn": (None, ["button"]),         # textContent === "Continuar con mi plan actual"
    },
    4: {
        "shipment_dropdown": ("shipmentDropdown",
                              ["#shipment-type-selection-dropdown-id-trigger",
                               '[role="combobox"]']),
        "vehicle_option": (None, ['[role="option"]']),  # textContent 含 "Vehículo"
        "date_input": ("dateInput", ['input[readonly]',
                                      'input[readonly][id^="_r_"]']),
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
# Playwright CDP 封装（替代 ziniao-cli subprocess）
# ────────────────────────────────────────────────

class PlaywrightClient:
    """Playwright CDP 统一封装：connect_over_cdp 直连紫鸟浏览器（默认 127.0.0.1:52420）。

    与旧 ZiniaoClient 的差异：
    - 不启动/关闭浏览器（浏览器归紫鸟所有，仅附着控制）
    - click()/fill() 原生触发 React onChange，无需 PointerEvent / execCommand / fiber hack
    - wait_for_selector / has_text filter 自动等待 React 渲染
    - 失败时可 screenshot 保存现场
    """

    def __init__(self, cfg: Config, selectors: Optional[Selectors] = None,
                 cdp_url: Optional[str] = None) -> None:
        self.cfg = cfg
        self._selectors = selectors
        self.cdp_url = cdp_url or cfg.cdp_url
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ---- 连接 ----

    @property
    def page(self):
        if self._page is None:
            raise StepError(0, "cli_error", "Playwright 尚未连接（先调用 connect()）")
        return self._page

    async def connect(self, step: int = 0, download_dir: Optional[str] = None) -> None:
        """连接 CDP 浏览器并定位 ML 页面（延迟连接：首次使用时调用）。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise StepError(step, "cli_error",
                            "playwright 未安装：请用 /tmp/pw-venv/bin/python3 运行"
                            "（python3.11 + playwright==1.62.0）") from exc
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as exc:
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            raise StepError(step, "cli_error",
                            f"CDP 连接失败 {self.cdp_url}: {exc}",
                            recovery_attempted=["connect_over_cdp"]) from exc
        # 设置下载目录（CDP 直连时浏览器不会自动使用 ziniao-cli store open 的目录）
        if download_dir:
            try:
                cdp = await self._browser.new_browser_cdp_session()
                await cdp.send("Browser.setDownloadBehavior", {
                    "behavior": "allow",
                    "downloadPath": download_dir,
                })
            except Exception:
                pass
        # 定位 ML 页面（跨所有 context）：优先 mercadolibre.com.mx
        target = None
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                if "mercadolibre.com.mx" in (p.url or ""):
                    target, self._context = p, ctx
                    break
            if target:
                break
        if not target:
            for ctx in self._browser.contexts:
                for p in ctx.pages:
                    if "mercadolibre.com" in (p.url or ""):
                        target, self._context = p, ctx
                        break
                if target:
                    break
        if not target and self._browser.contexts:
            self._context = self._browser.contexts[0]
            if self._context.pages:
                target = self._context.pages[0]
        if not target:
            raise StepError(step, "cli_error",
                            "CDP 浏览器中未找到 ML 页面（请先手动登录美客多）")
        self._page = target
        print(f"[{time.strftime('%H:%M:%S')}] ✅ CDP 已连接 {self.cdp_url}，新建页面",
              file=sys.stderr, flush=True)

    async def close(self) -> None:
        """断开 Playwright 驱动（不关闭紫鸟浏览器本身）。"""
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._context = self._page = None

    # ---- 页面导航 ----

    async def navigate(self, url: str, step: int = 0, wait_after: float = 0.0,
                       timeout: int = 60) -> None:
        # 层1自愈：goto 失败（ERR_CONNECTION_CLOSED 等）或成功后页面为紫鸟「网络修复」页
        # （chrome-extension://*/error.html）时，轮询等待紫鸟修复完成（最多 60s），
        # 再重新 goto 目标 URL（最多重试 2 次，每次重试前再等 5s）；重试仍失败才抛 StepError。
        last_exc: Optional[Exception] = None
        for attempt in range(AUTO_RETRY_MAX + 1):  # 首次 + 最多 2 次重试
            try:
                await self.page.goto(url, wait_until="domcontentloaded",
                                     timeout=timeout * 1000)
                last_exc = None
            except Exception as exc2:
                last_exc = exc2
                print(f"[{time.strftime('%H:%M:%S')}] ⚠️ goto 失败(第{attempt + 1}次): {exc2}",
                      file=sys.stderr, flush=True)
            # goto 成功或失败后统一检测紫鸟修复页（「成功」但内容是 error.html 也算失败）
            if await self._is_ziniao_error_page():
                print(f"[{time.strftime('%H:%M:%S')}] ⏳ 等待紫鸟网络修复...",
                      file=sys.stderr, flush=True)
                if await self._wait_ziniao_repaired():
                    await asyncio.sleep(5)  # 修复完成后等 5s 再重试
                continue
            if last_exc is None:
                break  # goto 成功且页面正常
            raise StepError(step, "timeout", f"页面导航失败: {url}: {last_exc}") from last_exc
        else:
            # 循环耗尽：3 次尝试全部命中紫鸟修复页且等待/重试未恢复
            raise StepError(step, "timeout",
                            f"页面导航失败（紫鸟网络修复未完成，error.html 仍存在）: {url}: "
                            f"{last_exc or ''}",
                            recovery_attempted=[ZINIA_NETWORK_RETRY_TAG])
        # 等待页面主体容器渲染完成（避免后续点击误触导航栏）
        try:
            await self.page.wait_for_selector("main, #root-app", timeout=15000)
        except Exception:
            pass
        # 等待旋转蒙层消失（比纯 sleep 更准确反映页面就绪；hidden 等待内部封顶 5s）
        await self._wait_spinner_gone()
        if wait_after:
            await asyncio.sleep(wait_after)

    async def _is_ziniao_error_page(self) -> bool:
        """检测当前页面是否为紫鸟「网络修复」页（chrome-extension://*/error.html）。

        URL 命中即真；否则取 body 文本前 500 字匹配修复关键词（goto 失败后页面
        URL 可能仍是旧页，此时以文本为准）。
        """
        try:
            url = self.page.url or ""
        except Exception:
            url = ""
        if ZINIA_ERROR_URL_MARK in url and ZINIA_ERROR_PATH_MARK in url:
            return True
        try:
            body = await self.page.evaluate(
                "() => (document.body ? document.body.innerText || '' : '').slice(0, 500)")
        except Exception:
            return False
        return any(k in body for k in ZINIA_ERROR_KEYWORDS)

    async def _wait_ziniao_repaired(self, timeout: float = 60.0,
                                    interval: float = 5.0) -> bool:
        """轮询等待紫鸟网络修复完成（error.html 消失），最多 timeout 秒（默认 60s/12 次）。

        返回 True=已恢复；False=超时未恢复。
        """
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(interval)
            waited += interval
            if not await self._is_ziniao_error_page():
                print(f"[{time.strftime('%H:%M:%S')}] ✅ 紫鸟网络已恢复",
                      file=sys.stderr, flush=True)
                return True
            print(f"[{time.strftime('%H:%M:%S')}] ⏳ 等待紫鸟网络修复... "
                  f"({int(waited)}s/{int(timeout)}s)",
                  file=sys.stderr, flush=True)
        return False

    async def verify_search_page(self) -> bool:
        """验证当前页面是 shipment_planning 搜索结果页（防紫鸟波动后窗口残留异常态跳错页）。

        URL 含 /shipment_planning/ 即通过（正确搜索 URL 本身带 /publicaciones/ 前缀，
        故不做「不含 publicaciones」判断，避免误杀）；URL 未命中时检查 body 是否出现
        Mis publicaciones 商品列表页特征（Algunas de tus publicaciones…）。
        """
        try:
            url = self.page.url or ""
        except Exception:
            url = ""
        if "/shipment_planning/" in url:
            return True
        try:
            body = await self.page.evaluate(
                "() => (document.body ? document.body.innerText || '' : '').slice(0, 500)")
        except Exception:
            body = ""
        return not any(m in body for m in SEARCH_WRONG_PAGE_MARKERS)

    async def _wait_spinner_gone(self) -> None:
        """等待 ML 页面旋转蒙层出现→消失（hidden 最多等 5s，常驻 loader 不阻塞导航）。

        实测（2026-08-06）：shipment_planning/plans 搜索页的 remote-module__loading /
        main-fetching-spinner 在 goto 后 ~6s 出现并持续可见 60-90s，而真实内容
        （结果表格 / 数量输入框）约 10-12s 就绪——spinner 只是过渡提示，不能作为
        页面就绪的硬性条件；页面就绪由后续步骤的 wait_for_selector / wait_for_text /
        wait_for_url 保证。hidden 等待封顶 5s，避免导航被常驻 loader 白等。
        """
        spinner = self.page.locator(
            '[class*=spinner], [class*=spin], [class*=loading], '
            '.andes-spinner, [role=progressbar], [aria-busy=true]'
        )
        try:
            await spinner.first.wait_for(state="visible", timeout=5000)
            await spinner.first.wait_for(state="hidden", timeout=5000)
        except Exception:
            pass

    async def visit_plan_page(self, path: str, shipment_id: str, step: int) -> None:
        """访问货件子页面：用货件号拼 URL。"""
        if shipment_id:
            url = f"{ML_BASE_URL}/shipping/inbounds/{shipment_id}/{path}"
        else:
            url = f"{ML_BASE_URL}/shipping/{path}"
        await self.navigate(url, step=step)

    # ---- 点击 ----

    async def click_selector(self, selector: str, step: int = 0,
                             wait_after: float = 0.0) -> str:
        """点击第一个匹配元素（无文本条件）。返回 'clicked' | 'notfound'。"""
        loc = self.page.locator(self._scope(selector))
        try:
            if await loc.count() == 0:
                return "notfound"
            await loc.first.click(timeout=15000)
        except Exception:
            return "notfound"
        if wait_after:
            await asyncio.sleep(wait_after)
        return "clicked"

    # --- 选择器作用域：避免误点顶部导航栏 ---
    _GENERIC_SELECTORS = ("input[type=checkbox]", 'input[type="checkbox"]')  # nav-header-user-switch 在导航栏

    def _scope(self, selector: str) -> str:
        """将通用选择器限定到页面主体区域（main），排除顶部导航栏。"""
        if selector in self._GENERIC_SELECTORS:
            return f"main {selector}, #root-app {selector}"
        return selector

    async def click_button(self, text: str, selector: str = "button",
                           require_enabled: bool = True, step: int = 0,
                           wait_after: float = 0.0, timeout: int = 15) -> str:
        """textContent 精确匹配点击（trim 语义，跳过 disabled）。返回 'clicked' | 'notfound'。

        require_enabled=False 时 force 点击（日历区 Confirmar 等 disabled 元素场景）。
        """
        pat = re.compile(rf"^\s*{re.escape(text)}\s*$")
        loc = self.page.locator(self._scope(selector)).filter(has_text=pat)
        try:
            count = await loc.count()
        except Exception:
            return "notfound"
        if count == 0:
            return "notfound"
        for i in range(count):
            el = loc.nth(i)
            if require_enabled:
                try:
                    if await el.is_disabled():
                        continue
                except Exception:
                    continue
            try:
                await el.click(timeout=timeout * 1000, force=not require_enabled)
                if wait_after:
                    await asyncio.sleep(wait_after)
                return "clicked"
            except Exception:
                continue
        return "notfound"

    async def click_contains(self, text: str, selector: str = "button",
                             step: int = 0, wait_after: float = 0.0) -> str:
        """textContent 包含匹配点击（如 "Descarga todas"）。返回 'clicked' | 'notfound'。"""
        loc = self.page.locator(selector).filter(has_text=text)
        try:
            count = await loc.count()
        except Exception:
            return "notfound"
        if count == 0:
            return "notfound"
        for i in range(count):
            el = loc.nth(i)
            try:
                if await el.is_disabled():
                    continue
            except Exception:
                continue
            try:
                await el.click(timeout=15000)
                if wait_after:
                    await asyncio.sleep(wait_after)
                return "clicked"
            except Exception:
                continue
        return "notfound"

    async def click_by_id(self, element_id: str, step: int = 0,
                          wait_after: float = 0.0) -> str:
        loc = self.page.locator(f"#{element_id}")
        try:
            if await loc.count() == 0:
                return "notfound"
            await loc.first.click(timeout=15000)
        except Exception:
            return "notfound"
        if wait_after:
            await asyncio.sleep(wait_after)
        return "clicked"

    async def click_role_option(self, role: str, text_contains: str, step: int = 0,
                                wait_after: float = 0.0) -> str:
        """打开下拉后选 option。返回 'selected' | 'notfound'。"""
        loc = self.page.locator(f'[role="{role}"]').filter(has_text=text_contains)
        try:
            if await loc.count() == 0:
                return "notfound"
            await loc.first.click(timeout=15000)
        except Exception:
            return "notfound"
        if wait_after:
            await asyncio.sleep(wait_after)
        return "selected"

    async def click_dialog_button(self, btn_text: str, step: int = 0,
                                  wait_after: float = 0.0) -> str:
        """任意 [role=dialog] 内点 btn_text。返回 'downloaded' | 'nodialog' | 'nobtn'。"""
        loc = self.page.locator('[role="dialog"] button').filter(
            has_text=re.compile(rf"^\s*{re.escape(btn_text)}\s*$"))
        try:
            count = await loc.count()
        except Exception:
            count = 0
        if count == 0:
            try:
                has_dialog = await self.page.locator('[role="dialog"]').count() > 0
            except Exception:
                has_dialog = False
            return "nobtn" if has_dialog else "nodialog"
        try:
            await loc.first.click(timeout=15000)
        except Exception:
            return "nobtn"
        if wait_after:
            await asyncio.sleep(wait_after)
        return "downloaded"

    async def click_modal_normal_and_button(self, title_contains: str, btn_text: str,
                                            step: int = 0, wait_after: float = 0.0) -> str:
        """在含 title_contains 的 [role=dialog] 内，先点叶子 'Normal'，再点 btn_text。

        返回 'downloaded' | 'nodialog' | 'nobtn'。
        """
        dialogs = self.page.locator('[role="dialog"]').filter(has_text=title_contains)
        try:
            if await dialogs.count() == 0:
                return "nodialog"
            dlg = dialogs.first
            normal = dlg.get_by_text("Normal", exact=True)
            if await normal.count() > 0:
                try:
                    await normal.first.click(timeout=8000)
                except Exception:
                    pass
            btn = dlg.locator("button").filter(
                has_text=re.compile(rf"^\s*{re.escape(btn_text)}\s*$"))
            if await btn.count() == 0:
                return "nobtn"
            await btn.first.click(timeout=15000)
        except Exception:
            return "nobtn"
        if wait_after:
            await asyncio.sleep(wait_after)
        return "downloaded"

    async def click_nth(self, selector: str, text: str, nth: int, step: int = 0,
                        wait_after: float = 0.0) -> str:
        """点击第 nth 个 textContent 精确匹配的元素（0-indexed）。返回 'clicked' | 'notfound'。"""
        loc = self.page.locator(selector).filter(
            has_text=re.compile(rf"^\s*{re.escape(text)}\s*$"))
        try:
            if await loc.count() <= nth:
                return "notfound"
            await loc.nth(nth).click(timeout=15000)
        except Exception:
            return "notfound"
        if wait_after:
            await asyncio.sleep(wait_after)
        return "clicked"

    # ---- 输入 ----

    async def fill_input(self, selector: str, value: str, step: int = 0,
                         wait_after: float = 0.0) -> str:
        """填充输入框（原生 fill 触发 React onChange）。返回 'filled' | 'notfound'。"""
        loc = self.page.locator(selector)
        try:
            if await loc.count() == 0:
                return "notfound"
            await loc.first.fill(value, timeout=15000)
        except Exception:
            return "notfound"
        if wait_after:
            await asyncio.sleep(wait_after)
        return "filled"

    async def press_enter(self, step: int = 0, wait_after: float = 0.0) -> None:
        await self.page.keyboard.press("Enter")
        if wait_after:
            await asyncio.sleep(wait_after)

    async def click_checkboxes(self, selector: str, step: int = 0,
                               wait_after: float = 0.0,
                               nth_start: Optional[int] = None,
                               nth_end: Optional[int] = None) -> str:
        """勾选选择器下的 checkbox（原生 click，React onChange 自动触发）。

        - 已是 checked 的跳过（幂等，与旧脚本 set checked=true 语义一致）
        - 隐藏 input（Andes 风格）用 DOM click 兜底
        - 容器/label 优先原生点击容器本身（Andes 的 onClick 常在容器上）
        - nth_start/nth_end 支持只勾选区间（如箱唛 Pallets #2/#3）
        返回 'checked:N/M' | 'need3:N' | 'notfound'。
        """
        loc = self.page.locator(self._scope(selector))
        try:
            count = await loc.count()
        except Exception:
            count = 0
        if count == 0:
            return "notfound"
        end = min(nth_end if nth_end is not None else count, count)
        if nth_start is not None and count < (nth_end if nth_end is not None else count):
            return f"need3:{count}"
        start = nth_start or 0
        clicked = 0
        for i in range(start, end):
            el = loc.nth(i)
            try:
                tag = await el.evaluate("(el) => el.tagName")
            except Exception:
                continue
            try:
                if tag == "INPUT":
                    if await el.is_visible():
                        if await el.is_checked():
                            clicked += 1
                            continue
                        await el.click(timeout=8000)
                    else:
                        await el.evaluate("(el) => el.click()")
                    clicked += 1
                    continue
                # 容器/label
                if await el.is_visible():
                    inner = el.locator("input[type=checkbox]")
                    if await inner.count() > 0 and await inner.first.is_checked():
                        clicked += 1
                        continue
                    await el.click(timeout=8000)
                else:
                    inner = el.locator("input[type=checkbox]")
                    if await inner.count() > 0:
                        await inner.first.evaluate("(el) => el.click()")
                    else:
                        await el.evaluate("(el) => el.click()")
                clicked += 1
            except Exception:
                continue
        if wait_after:
            await asyncio.sleep(wait_after)
        return f"checked:{clicked}/{count}"

    # ---- 等待 / 读取 ----

    async def wait_for_selector(self, selector: str, timeout: int = 30,
                                step: int = 0, action: str = "") -> None:
        try:
            await self.page.wait_for_selector(selector, timeout=timeout * 1000)
        except Exception as exc:
            raise StepError(step, "timeout",
                            f"[{action}] 等待选择器超时({timeout}s): {selector}") from exc

    async def wait_for_text(self, text: str, selector: str = "*", timeout: int = 30,
                            step: int = 0, action: str = "") -> None:
        loc = self.page.locator(selector).filter(has_text=text)
        try:
            await loc.first.wait_for(state="visible", timeout=timeout * 1000)
        except Exception as exc:
            raise StepError(step, "timeout",
                            f"[{action}] 等待文本超时({timeout}s): {text!r}") from exc

    async def evaluate(self, js: str, step: int = 0, wait_after: float = 0.0) -> str:
        """执行页面 JS（用于复杂操作：提取 ML 码/货件号/表格数据）。返回字符串。"""
        try:
            result = await self.page.evaluate(js)
        except Exception as exc:
            raise StepError(step, "cli_error", f"页面 JS 执行失败: {exc}") from exc
        if wait_after:
            await asyncio.sleep(wait_after)
        return "" if result is None else str(result)

    async def current_url(self) -> str:
        return self.page.url

    async def screenshot(self, path: str) -> str:
        """调试用：保存页面截图。"""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        await self.page.screenshot(path=path)
        return path

    # ---- fallback 链（沿用 STEP_FALLBACKS 语义）----

    def _chain(self, step: int, action: str) -> list[str]:
        return self._selectors.chain(step, action) if self._selectors else [""]

    async def click_with_fallback(self, step: int, action: str, text: str,
                                  require_enabled: bool = True,
                                  wait_after: float = 0.0) -> str:
        """精确文本点击，按 fallback 链逐个选择器尝试；全部失败抛 StepError。"""
        chain = self._chain(step, action)
        recovery: list[str] = []
        for idx, selector in enumerate(chain):
            for _attempt in range(2):
                r = await self.click_button(text, selector, require_enabled=require_enabled,
                                            step=step, wait_after=wait_after)
                if r == "clicked":
                    return r
                await asyncio.sleep(0.8)
            if idx < len(chain) - 1:
                recovery.append(f"alt_selector:{action}@{idx + 1}")
        raise StepError(step, "selector_not_found",
                        f"[{action}] 未找到目标元素（fallback 链 {len(chain)} 个选择器全部失败）",
                        recovery_attempted=recovery)

    async def click_contains_with_fallback(self, step: int, action: str, text: str,
                                           wait_after: float = 0.0) -> str:
        """包含匹配点击（如 Descarga todas），fallback 链尝试。"""
        chain = self._chain(step, action)
        recovery: list[str] = []
        for idx, selector in enumerate(chain):
            for _attempt in range(2):
                r = await self.click_contains(text, selector, step=step, wait_after=wait_after)
                if r == "clicked":
                    return r
                await asyncio.sleep(0.8)
            if idx < len(chain) - 1:
                recovery.append(f"alt_selector:{action}@{idx + 1}")
        raise StepError(step, "selector_not_found",
                        f"[{action}] 未找到目标元素（fallback 链 {len(chain)} 个选择器全部失败）",
                        recovery_attempted=recovery)

    async def click_selector_with_fallback(self, step: int, action: str,
                                           wait_after: float = 0.0) -> str:
        """无文本条件点击（下拉 trigger/日期输入框等），fallback 链尝试。"""
        chain = self._chain(step, action)
        recovery: list[str] = []
        for idx, selector in enumerate(chain):
            r = await self.click_selector(selector, step=step, wait_after=wait_after)
            if r == "clicked":
                return r
            if idx < len(chain) - 1:
                recovery.append(f"alt_selector:{action}@{idx + 1}")
        raise StepError(step, "selector_not_found",
                        f"[{action}] 未找到目标元素（fallback 链 {len(chain)} 个选择器全部失败）",
                        recovery_attempted=recovery)

    async def fill_with_fallback(self, step: int, action: str, value: str,
                                 press_enter: bool = False,
                                 wait_after: float = 0.0) -> str:
        """填充输入框（可选 Enter 提交），fallback 链尝试。"""
        chain = self._chain(step, action)
        recovery: list[str] = []
        for idx, selector in enumerate(chain):
            r = await self.fill_input(selector, value, step=step)
            if r == "filled":
                if press_enter:
                    await self.press_enter(step=step, wait_after=wait_after)
                elif wait_after:
                    await asyncio.sleep(wait_after)
                return r
            if idx < len(chain) - 1:
                recovery.append(f"alt_selector:{action}@{idx + 1}")
        raise StepError(step, "selector_not_found",
                        f"[{action}] 未找到输入框（fallback 链 {len(chain)} 个选择器全部失败）",
                        recovery_attempted=recovery)

    async def click_checkboxes_with_fallback(self, step: int, action: str,
                                             nth_start: Optional[int] = None,
                                             nth_end: Optional[int] = None,
                                             wait_after: float = 0.0) -> str:
        """勾选 checkbox，fallback 链尝试。返回 'checked:N/M' | 'need3:N' | 'notfound'。"""
        chain = self._chain(step, action)
        recovery: list[str] = []
        for idx, selector in enumerate(chain):
            r = await self.click_checkboxes(selector, step=step, wait_after=wait_after,
                                            nth_start=nth_start, nth_end=nth_end)
            if r.startswith("checked") or r.startswith("need3"):
                return r
            if idx < len(chain) - 1:
                recovery.append(f"alt_selector:{action}@{idx + 1}")
        return "notfound"


# ────────────────────────────────────────────────
# lark-cli 封装（飞书多维表格 + 消息）
# ────────────────────────────────────────────────

class FeishuClient:
    """lark-cli 封装：Pending 记录查询、字段更新、附件上传、消息推送。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _run(self, args: list[str], timeout: int = 60, retries: int = 2,
             cwd: Optional[str] = None) -> str:
        last_err = ""
        for attempt in range(retries):
            try:
                proc = subprocess.run(["lark-cli", *args],
                                      capture_output=True, text=True, timeout=timeout,
                                      cwd=cwd)
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
        """查询 状态 in (Pending, 运行中) 且 就绪=true 的记录（与 poll-fulfillment.sh 同口径）。

        运行中+就绪=true = 上次中断残留（异常未走失败分支），下轮续跑；就绪=false 不捡。
        """
        if not self.cfg.feishu_ready:
            raise StepError(0, "business", "飞书未配置（缺 FEISHU_BASE_TOKEN/FEISHU_TABLE_ID）")
        filter_json = json.dumps({"logic": "or",
                                  "conditions": [["状态", "==", "Pending"],
                                                 ["状态", "==", "运行中"]]})
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
            if ready and any(k in str(s) for k in ("Pending", "运行中")
                             for s in statuses if s is not None):
                records.append({
                    "record_id": ids[i],
                    "sku": str(f.get("SKU", "") or ""),
                    "name": str(f.get("品名", "") or ""),
                    "qty": str(f.get("数量", "") or ""),
                    "box": str(f.get("箱数", "") or ""),
                    "shipment_id": str(f.get("货件号") or ""),
                    # lark-cli 单选字段返回 list（如 ['2店-子账号']），取首元素与 stores.json key 匹配
                    "store_name": str((f.get("店铺名称") or [""])[0]
                                      if isinstance(f.get("店铺名称"), list) and f.get("店铺名称")
                                      else f.get("店铺名称") or ""),
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
        # lark-cli 要求 --file 必须是相对路径，先 cd 到文件所在目录
        p = Path(file_path)
        self._run(["base", "+record-upload-attachment",
                   "--base-token", self.cfg.base_token,
                   "--table-id", self.cfg.table_id,
                   "--record-id", record_id,
                   "--field-id", field_name,
                   "--file", p.name], timeout=120, cwd=str(p.parent))

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
# JS 片段（仅保留数据提取类；交互全部用 Playwright 原生 click/fill）
# ────────────────────────────────────────────────

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

JS_EXTRACT_ML_CODE = """(function(){
  var tds = document.querySelectorAll('td');
  for (var i = 0; i < tds.length; i++) {
    var t = tds[i].textContent.trim();
    var m = t.match(/[A-Z]{4}[0-9]+/) || t.match(/ML[UB][0-9]+/);
    if (m) return m[0];
  }
  // 兜底：搜 Código ML: XXXX12345 格式
  var body = document.body.textContent;
  var m2 = body.match(/Código ML:\s*([A-Z0-9]+)/);
  if (m2) return m2[1];
  return 'UNKNOWN';
})();"""

JS_EXTRACT_SHIPMENT_ID = """(function(){
  // 优先从页面 DOM 提取货件号（URL 中的 /inbounds/ 编号 ≠ 货件号）
  var h1 = document.querySelector('h1, h2, [class*=title]');
  if (h1) {
    var m = h1.textContent.match(/#?(\d{8,})/);
    if (m) return m[1];
  }
  // 兜底：URL /plans/XXXXX 或页面任意 8+ 位数字
  var pm = location.href.match(/\/plans\/(\d+)/);
  if (pm) return pm[1];
  var body = document.body.textContent;
  var bm = body.match(/envío\s*#?(\d{8,})/i) || body.match(/shipment\s*#?(\d{8,})/i);
  if (bm) return bm[1];
  return 'UNKNOWN';
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
    store_name: str = ""
    inbound_id: str = ""
    completed_steps: list[int] = field(default_factory=list)
    files_uploaded: dict[str, str] = field(default_factory=dict)
    dry_run_notes: list[str] = field(default_factory=list)


class Orchestrator:
    """8 步 FULL 货件流程编排器。"""

    def __init__(self, cfg: Config, selectors: Selectors) -> None:
        self.cfg = cfg
        self.sel = selectors
        self.browser = PlaywrightClient(cfg, selectors)
        self.feishu = FeishuClient(cfg)
        self.state = RunState()
        self._dl_dir: Optional[Path] = None  # 由 _open_store() 设置（store open 返回的下载目录）
        self._store_lock: Optional[Any] = None  # 店铺级 flock 锁 fd（run() 中获取，进程退出自动释放）

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

    async def _screenshot_on_error(self, step: int) -> Optional[str]:
        """步骤失败时自动截图现场到 /tmp/fulfillment-screenshots/。

        文件名带 SKU（step{step}-{sku}-{YYYYMMDD-HHMMSS}.png）便于与失败记录关联；
        SKU 为空时保持原格式。返回截图绝对路径（失败返回 None）。
        """
        try:
            d = Path("/tmp/fulfillment-screenshots")
            d.mkdir(parents=True, exist_ok=True)
            sku = (self.state.sku or "").strip()
            if sku:
                safe_sku = re.sub(r"[^\w.\-]", "_", sku)
                p = d / f"step{step}-{safe_sku}-{time.strftime('%Y%m%d-%H%M%S')}.png"
            else:
                p = d / f"step{step}-{time.strftime('%Y%m%d-%H%M%S')}.png"
            await self.browser.screenshot(str(p))
            self._log(f"  📸 失败现场截图: {p}")
            return str(p)
        except Exception as exc:
            self._log(f"  ⚠️ 失败截图不可用: {exc}")
            return None

    # ---- 本地失败诊断（failures.jsonl 索引）----
    def _current_log_file(self) -> str:
        """本次运行日志路径：优先 poll 脚本注入的 FULFILLMENT_LOG_FILE，兜底按店铺名推导。"""
        env_path = os.environ.get("FULFILLMENT_LOG_FILE", "").strip()
        if env_path:
            return env_path
        store = self.state.store_name or self.cfg.store_name or "unknown"
        safe_store = re.sub(r"[^\w.\-]", "_", store)
        return str(FAILURES_DIR / f"run-{time.strftime('%Y-%m-%d')}-{safe_store}.log")

    async def _capture_page_diag(self) -> tuple[str, str]:
        """抓取失败现场：当前页 URL + DOM 摘要（主容器文本前 300 字 + 关键元素计数）。"""
        try:
            page = self.browser.page
            url = page.url or ""
            summary = await page.evaluate(
                """() => {
                    const main = document.querySelector('main, #root-app') || document.body;
                    const text = (main ? main.innerText || '' : '').replace(/\\s+/g, ' ').trim();
                    const n = (s) => document.querySelectorAll(s).length;
                    return JSON.stringify({
                        text: text.substring(0, 300),
                        inputs: n('input'),
                        buttons: n('button'),
                        days: n('div.day'),
                        hours: n('div.hour')
                    });
                }"""
            )
            data = json.loads(summary)
            dom_summary = (
                f"text={data.get('text', '')} "
                f"| input={data.get('inputs', 0)} button={data.get('buttons', 0)} "
                f"| div.day={data.get('days', 0)} div.hour={data.get('hours', 0)}"
            )
            return url, dom_summary
        except Exception:
            return "", ""

    def _record_failure(self, step: int, exc: StepError, *,
                        screenshot: Optional[str] = None,
                        page_url: str = "", dom_summary: str = "") -> None:
        """追加一条失败诊断记录到 failures.jsonl（纯本地；写失败静默，不影响主流程）。"""
        try:
            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "store": self.state.store_name or self.cfg.store_name or "",
                "sku": self.state.sku or "",
                "name": self.state.name or "",
                "qty": self.state.qty or "",
                "box": self.state.box or "",
                "shipment_id": self.state.shipment_id or "",
                "record_id": self.state.record_id or "",
                "failed_step": step,
                "error_type": exc.err_type,
                "error_message": exc.message,
                "recovery_attempted": exc.recovery_attempted or [],
                "screenshot": screenshot or "",
                "log_file": self._current_log_file(),
                "page_url": page_url,
                "dom_summary": dom_summary,
            }
            _append_failure(record)
            self._log(f"  🗂️ 已记录失败诊断: {FAILURES_JSONL}")
        except Exception as exc2:
            self._log(f"  ⚠️ 失败诊断记录失败: {exc2}")

    # ---- Telemetry 云上报（opt-in：telemetry_opt_in=true 且有 auth_token 才 POST）----
    def _friendly_failure_message(self) -> str:
        """用户友好失败文案：技术细节不再推给客户（诊断走本地 failures.jsonl + 云上报）。"""
        return (f"😔 FULL 货物处理中断（{self.state.sku or '未知SKU'} "
                f"{self.state.name or ''}）。已记录诊断信息，请联系问述科技支持排查。")

    def _fail_record(self, step: int, exc: StepError, *, dry_run: bool = False) -> None:
        """统一失败分支：写 状态=失败 / 就绪=false / 当前步骤=失败信息 / 用户友好消息
        + failures.jsonl + 云上报。

        防死锁核心：任何异常（StepError 或环境类非 StepError）只要已把记录标记为
        「运行中」，必须经此改回 失败+就绪=false，否则 poll 永不重新捡取该记录。
        dry_run 保持零副作用（与「运行中」标记同规则）。
        """
        if self.state.record_id and not dry_run:
            self.feishu.update_field(self.state.record_id, "状态", "失败")
            self.feishu.update_field(self.state.record_id, "就绪", False)
            self.feishu.update_step(self.state.record_id, f"失败：{exc.message}")
            self.feishu.send_message(self._friendly_failure_message())
        self._record_failure(step, exc)
        self._report_failure(step, exc)

    def _report_failure(self, step: int, exc: StepError, *,
                        screenshot: Optional[str] = None,
                        page_url: str = "", dom_summary: str = "") -> None:
        """失败诊断包上报 whyshu-svr 云 API（POST /api/argent/telemetry/failure）。

        opt-in 关闭 / auth_token 缺失 / 网络或服务端错误 → 一律静默（try/except），
        绝不阻断 FULL 主流程；与本地 failures.jsonl 记录共用同一现场数据。
        """
        if not _read_telemetry_opt_in():
            return
        token = _read_auth_token()
        if not token:
            return
        body: dict[str, Any] = {
            "version": _read_skill_version(),
            "ts": datetime.now(timezone.utc).isoformat(),
            "store": self.state.store_name or self.cfg.store_name,
            "sku": self.state.sku,
            "name": self.state.name,
            "qty": self.state.qty,
            "box": self.state.box,
            "shipment_id": self.state.shipment_id,
            "record_id": self.state.record_id,
            "failed_step": step,
            "error": exc.to_dict(),
            "log_tail": _read_log_tail(self._current_log_file()),
            "screenshot_base64": _screenshot_base64(screenshot),
            "page_url": page_url,
            "dom_summary": dom_summary,
        }
        try:
            req = urllib.request.Request(
                TELEMETRY_URL,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + token},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    self._log("📡 telemetry 失败诊断包已上报")
        except Exception as exc2:
            self._log(f"  ⚠️ telemetry 上报失败(忽略): {exc2}")

    # ---- 网络类错误自动重置（层2 cron 兜底：保留货件号续传，最多 AUTO_RETRY_MAX 次）----
    def _is_network_error(self, exc: StepError, *, page_url: str = "",
                          dom_summary: str = "") -> bool:
        """判断是否紫鸟网络类错误：失败现场为修复页，或 timeout 且消息含网络关键词。

        选择器/业务类错误绝不判为网络错误（防重复写操作）。
        """
        if _ziniao_error_by_diag(page_url, dom_summary):
            return True
        msg = exc.message or ""
        return (exc.err_type in ("timeout",)
                and any(k in msg for k in ("ERR_CONNECTION_CLOSED", "error.html",
                                           "网络", ZINIA_NETWORK_RETRY_TAG)))

    def _auto_retry_count(self, record_id: str) -> int:
        """读取 failures.jsonl 中该 record 的自动重试标记数。

        重试次数持久化在本地 failures.jsonl（event=auto_retry 行），不依赖 Base
        新增字段——避免客户飞书表格缺字段导致次数永远读不到、无限重置。
        """
        try:
            count = 0
            with open(FAILURES_JSONL, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("event") == "auto_retry" and d.get("record_id") == record_id:
                        count += 1
            return count
        except Exception:
            return 0

    def _append_auto_retry(self, record_id: str, retry_no: int) -> None:
        """追加自动重试标记到 failures.jsonl（与失败诊断同文件，event=auto_retry 区分）。"""
        try:
            _append_failure({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": "auto_retry",
                "record_id": record_id,
                "store": self.state.store_name or self.cfg.store_name or "",
                "sku": self.state.sku or "",
                "shipment_id": self.state.shipment_id or "",
                "retry_no": retry_no,
            })
        except Exception:
            pass

    def _maybe_auto_reset(self, step: int, exc: StepError, *,
                          page_url: str = "", dom_summary: str = "") -> Optional[int]:
        """网络类错误自动重置记录（层2 cron 兜底，最多 AUTO_RETRY_MAX 次）。

        仅限网络类错误（timeout / ERR_CONNECTION_CLOSED / 紫鸟修复页）；选择器/业务类
        错误绝不自动重置（防重复写操作）。重置动作：
          状态 → Pending、就绪 → true、当前步骤 → 空；
          ⚠️ 货件号字段【保留不动】——有货件号则下轮跳过步骤3从步骤4续传，
          无货件号从头跑。
        返回本次重试序号（1/2）表示已重置；返回 None 表示未触发（无记录 /
        非网络类错误 / 已达重试上限）。
        """
        record_id = self.state.record_id
        if not record_id:
            return None
        if not self._is_network_error(exc, page_url=page_url, dom_summary=dom_summary):
            self._log("  ℹ️ 非网络类错误，不触发自动重置（走人工流程）")
            return None
        retry_count = self._auto_retry_count(record_id)
        if retry_count >= AUTO_RETRY_MAX:
            self._log(f"  ⛔ 网络类错误但已达自动重试上限（{retry_count}/{AUTO_RETRY_MAX}），"
                      f"保持失败，走人工流程")
            return None
        n = retry_count + 1
        # ⚠️ 只 patch 状态/就绪/当前步骤，绝不触碰货件号（续传前提）
        self.feishu.update_field(record_id, "状态", "Pending")
        self.feishu.update_field(record_id, "就绪", True)
        self.feishu.update_step(record_id, "")
        self._append_auto_retry(record_id, n)
        self.feishu.send_message(
            f"⚠️ 网络波动自动重试（第 {n} 次），下轮 cron 自动续传"
            + (f"（货件号 {self.state.shipment_id} 保留）" if self.state.shipment_id else ""))
        self._log(f"  🔄 网络波动自动重试（第 {n} 次）：记录 {record_id} 重置为 Pending，"
                  f"货件号{'保留' if self.state.shipment_id else '无，从头跑'}")
        return n

    async def _wait_any_selector(self, step: int, action: str, chain: list[str],
                                 timeout_first: int = 24, timeout_rest: int = 10) -> None:
        """按 fallback 链依次等待任一选择器出现；全部超时抛 StepError。"""
        for idx, sel in enumerate(chain):
            try:
                await self.browser.wait_for_selector(
                    sel, timeout=timeout_first if idx == 0 else timeout_rest,
                    step=step, action=action)
                return
            except StepError:
                continue
        raise StepError(step, "timeout",
                        f"[{action}] 等待页面元素超时（fallback 链 {len(chain)} 个选择器全部失败）",
                        recovery_attempted=[f"alt_selector:{action}@{len(chain)}"])

    # ==================================================
    # 步骤 1：前期准备（只读）
    # ==================================================
    async def step1_prepare(self) -> dict[str, Any]:
        self._log("步骤1 前期准备：检查 FULL 管理页")
        if self.state.shipment_id:
            # 有货件号：直接 URL query 查目标货件状态
            await self.browser.navigate(
                f"{INBOUNDS_URL}?query={self.state.shipment_id}", step=1, wait_after=2.0)
            out = await self.browser.evaluate(JS_EXTRACT_SHIPMENTS, step=1)
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                data = {"shipments": [], "capacity_warning": None}
            status = ""
            for s in data.get("shipments", []):
                if s["id"] == self.state.shipment_id:
                    status = s.get("status", "")
                    if status in ("Reserva cancelada", "Procesamiento finalizado", "Cancelado"):
                        self._log(f"  货件 #{self.state.shipment_id} 状态={status}，已走完流程")
                        if self.state.record_id:
                            self.feishu.update_field(self.state.record_id, "状态", "已完成")
                            self.feishu.update_step(self.state.record_id, "全部完成（已有终态）")
                            self.feishu.update_field(self.state.record_id, "就绪", False)
                        return {"status": "already_completed", "shipment_status": status}
                    break
            self._mark_done(1)
            return {"shipment_status": status}
        # 无货件号：仅导航到 inbounds 页确认 CDP 可用，不做全表扫描
        await self.browser.navigate(INBOUNDS_URL, step=1, wait_after=0.5)
        self._mark_done(1)
        return {}

    # ==================================================
    # 步骤 2：点击 Enviar productos 进入创建入口（只读导航）
    # ==================================================
    async def step2_entry(self) -> None:
        self._log("步骤2 创建入口（步3直接URL搜索，此步保留兼容）")
        self._mark_done(2)

    # ==================================================
    # 步骤 3：选择产品与数量（写操作）
    # ==================================================
    async def step3_select_product(self) -> None:
        self._log("步骤3 选择产品与数量")
        # 3a. 直接用 URL query 搜索 SKU
        search_url = f"https://www.mercadolibre.com.mx/publicaciones/listado/shipment_planning/plans?search={self.state.sku}"
        await self.browser.navigate(search_url, step=3, wait_after=1.0)
        # 3a2. 验证页面确实是 shipment_planning 搜索结果页（防紫鸟波动后窗口残留异常态跳错页）；
        #      navigate() 自愈后 URL 应已正确，此验证是第二道保险
        if not await self.browser.verify_search_page():
            self._log("  ⚠️ 搜索后页面不是搜索结果页（疑似跳错页），重新导航 1 次")
            await self.browser.navigate(search_url, step=3, wait_after=1.0)
            if not await self.browser.verify_search_page():
                raise StepError(3, "business",
                                "搜索后页面被跳转到非搜索结果页（疑似网络波动后窗口残留异常态）",
                                recovery_attempted=["search_page_verify"])
        self._log(f"  SKU {self.state.sku} 已搜索")
        # 3b. 检测搜索结果：仅看是否出现 "0 resultados"
        await asyncio.sleep(2)
        has_results = await self.browser.page.evaluate(
            """() => !/0\\s*resultados/i.test(document.body.textContent || '')"""
        )
        if not has_results:
            msg = f"SKU {self.state.sku} 在店铺 {self.state.store_name} 未找到"
            exc = StepError(3, "business", msg, recovery_attempted=["check_sku"])
            self._log(f"  ⚠️ {msg}")
            screenshot = await self._screenshot_on_error(3)
            page_url, dom_summary = await self._capture_page_diag()
            self._record_failure(3, exc, screenshot=screenshot,
                                 page_url=page_url, dom_summary=dom_summary)
            self._report_failure(3, exc, screenshot=screenshot,
                                 page_url=page_url, dom_summary=dom_summary)
            if self.state.record_id:
                self.feishu.update_field(self.state.record_id, "状态", "失败")
                self.feishu.update_field(self.state.record_id, "就绪", False)
                self.feishu.update_step(self.state.record_id, f"失败：{msg}")
                self.feishu.send_message(self._friendly_failure_message())
            raise exc
        # 3c. 等搜索结果页内容渲染完成（数量输入框出现，最长 25s——ML 搜索页异步渲染约 10-12s）
        try:
            await self.browser.page.wait_for_selector(
                'input[class*="andes-form-control"]:not([placeholder])', timeout=25000)
        except Exception:
            pass  # 输入框已存在或选择器不匹配，fill_with_fallback 会兜底
        # 关掉可能存在的广告弹窗/教程蒙层（循环 2 次：广告关闭后可能再出现 coachmarks）
        for _ in range(2):
            await self._dismiss_overlay()
            await asyncio.sleep(0.8)
        await self.browser.fill_with_fallback(3, "qty_input", self.state.qty, wait_after=1.0)
        self._log("  等待 Continuar 按钮启用...")
        # 3d. 等 Continuar 按钮「可用」（精确文本 + 非 disabled + 可见），最长 30s
        #     注意：has-text 会匹配 disabled 的加载中按钮（存在即通过，1s 就往下走），
        #     但 click_with_fallback 要求 textContent 精确 === Continuar 且 !disabled，
        #     等待条件与点击条件不一致 → disabled 时点击 notfound。必须轮询等 !disabled。
        for _ in range(30):
            ok = await self.browser.page.evaluate('''() => {
                const btns = Array.from(document.querySelectorAll('button'));
                return btns.some(b => {
                    const t = (b.textContent || '').trim();
                    const r = b.getBoundingClientRect();
                    return t === 'Continuar' && !b.disabled && r.width > 0 && r.height > 0;
                });
            }''')
            if ok:
                break
            await asyncio.sleep(1)
        else:
            # 30s 仍未启用：截图由外层失败处理兜底，抛 StepError
            raise StepError(3, "selector_not_found", "Continuar 按钮 30s 内未启用（仍 disabled）",
                            recovery_attempted=["wait_enabled_30s"])
        await self._dismiss_overlay()  # 点击前再清一次蒙层
        await self.browser.click_with_fallback(3, "continuar_btn", "Continuar", wait_after=2.0)
        modal_chain = self.sel.chain(3, "plan_modal_btn")
        for idx, sel in enumerate(modal_chain):
            try:
                await self.browser.wait_for_text("Continuar con mi plan actual", selector=sel,
                                                 timeout=20 if idx == 0 else 8,
                                                 step=3, action="plan_modal")
                break
            except StepError:
                continue
        else:
            raise StepError(3, "timeout", "等待弹窗 Continuar con mi plan actual 超时",
                            recovery_attempted=["poll_10x"])
        await self.browser.click_with_fallback(3, "plan_modal_btn",
                                               "Continuar con mi plan actual", wait_after=3.0)
        self._log("  等待创建完成并跳转到 hub...")
        try:
            await self.browser.page.wait_for_url("**/hub-v2**", timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(2)
        # 3e. 货件号
        shipment = await self.browser.evaluate(JS_EXTRACT_SHIPMENT_ID, step=3)
        if shipment == "UNKNOWN" or not shipment:
            raise StepError(3, "business", "未从页面提取到货件号", recovery_attempted=["dom_regex"])
        self.state.shipment_id = shipment
        try:
            self.state.inbound_id = str(int(shipment) - 1)
        except ValueError:
            pass
        self._log(f"  货件号: {shipment}" + (f" inbound={self.state.inbound_id}" if self.state.inbound_id else ""))
        self.feishu.update_field(self.state.record_id, "货件号", shipment)
        self._mark_done(3)
        if self.state.record_id:
            self.feishu.send_message(f"✅ 步骤1-3完成: 货件 #{shipment} {self.state.sku} {self.state.qty}件")

    # ==================================================
    # 步骤 4：货件预约时间（写操作）
    # ==================================================
    async def step4_appointment(self) -> None:
        self._log("步骤4 货件预约时间")
        # 4a. 进入预约页（优先用货件号拼 URL，其次从 URL 提取 inbound ID）
        await self.browser.visit_plan_page("appointment-v2", self.state.shipment_id, step=4)
        # 等待预约页加载（轮询运输方式下拉框）
        dd_chain = self.sel.chain(4, "shipment_dropdown")
        await self._wait_any_selector(4, "appointment_page", dd_chain)
        # 4b. 运输方式下拉（原生点击 trigger 打开 → 选 Vehículo particular）
        await self.browser.click_selector_with_fallback(4, "shipment_dropdown", wait_after=2.0)
        r = await self.browser.click_role_option("option", "Vehículo", step=4)
        if r != "selected":
            raise StepError(4, "selector_not_found", "未找到 Vehículo 运输选项",
                            recovery_attempted=["alt_selector:vehicle_option"])
        self._log("  配送方式: Vehículo particular")
        # 等页面 React 渲染完成
        await asyncio.sleep(1)
        # 检测是否出现地址选择（大货件需要选仓库地址）
        try:
            await self.browser.page.wait_for_selector('.andes-list__item-action', timeout=5000)
            addr_btns = self.browser.page.locator('.andes-list__item-action')
            if await addr_btns.count() > 0:
                self._log("  检测到大货件地址选择，选 MXCD05")
                for i in range(await addr_btns.count()):
                    btn = addr_btns.nth(i)
                    if await btn.is_visible():
                        text = (await btn.text_content() or "")
                        if "MXCD05" in text:
                            await btn.click()
                            self._log("  已选地址: MXCD05")
                            await asyncio.sleep(1)
                            await self.browser.page.locator(
                                '.multi-node-card button, .andes-card__footer button'
                            ).filter(has_text="Continuar").first.click(force=True)
                            self._log("  已点 Continuar")
                            break
        except Exception:
            pass
        # 等日期输入框
        # 等待页面稳定（Vehículo 选择后 React 重渲染）
        await self.browser.wait_for_selector('input[readonly]', timeout=15, action="date_input_after_vehicle")
        # 4c. 日期选择：点击只读日期输入框打开日历，然后灰圈算法选第 30 格
        await self.browser.click_selector_with_fallback(4, "date_input")
        await asyncio.sleep(1)
        picked = await self._pick_date()
        self._log(f"  预约日期: {picked}")
        # 4d. 时间选择（第一个 div.hour）
        hour_chain = self.sel.chain(4, "hour")
        loc = self.browser.page.locator(hour_chain[0])
        try:
            if await loc.count() == 0:
                raise StepError(4, "selector_not_found", "时间选择失败",
                                recovery_attempted=["alt_selector:hour"])
            hour_text = (await loc.first.text_content() or "").strip()
            await loc.first.click(timeout=15000)
        except StepError:
            raise
        except Exception as exc:
            raise StepError(4, "selector_not_found", f"时间选择失败: {exc}",
                            recovery_attempted=["alt_selector:hour"]) from exc
        await asyncio.sleep(1.0)
        self._log(f"  预约时间: {hour_text}")
        # 4e. 确认（分两次：第1次点日历区 Confirmar 注册日期，第2次点主确认提交预约）
        confirm_chain = self.sel.chain(4, "confirm_btn")
        r = await self.browser.click_button("Confirmar", confirm_chain[0],
                                            require_enabled=False, step=4)
        if r != "clicked":
            raise StepError(4, "selector_not_found", "未找到第1个 Confirmar（日历区）",
                            recovery_attempted=["retry_3x"])
        # 4e-1. 输入框写入验证（替代/叠加原 sleep(2)）：日历区确认后日期时间必须写入只读输入框
        #       （如 "7 de septiembre, 2:00 hs"）。未写入 → 重试日历区 Confirmar（最多 2 次，
        #       间隔 1.5s，重试后重新验证）→ 仍为空抛 business 错，避免空表单提交。
        async def _ensure_input_written() -> None:
            if await self._wait_appointment_input_written():
                return
            for attempt in range(2):
                self._log(f"  ⚠️ 日期时间输入框为空，重试点击日历区 Confirmar ({attempt + 1}/2)")
                await asyncio.sleep(1.5)
                await self.browser.click_button("Confirmar", confirm_chain[0],
                                                require_enabled=False, step=4)
                if await self._wait_appointment_input_written():
                    return
            raise StepError(4, "business", "日历区确认未写入日期时间（输入框为空）",
                            recovery_attempted=["calendar_confirm_retry"])

        await _ensure_input_written()
        self._log(f"  日历区确认已写入日期时间: {await self._appointment_input_value()}")
        # 第2次：点主确认——日历区确认后主确认是页面上（top>700 的）Confirmar，
        # 不能再用 confirm_chain[0]（那仍是日历区按钮）
        main_btn = self.browser.page.locator("button", has_text="Confirmar").last
        try:
            box = await main_btn.bounding_box()
            if not box or box["y"] < 700:
                raise StepError(4, "selector_not_found", "主 Confirmar 不可用或未找到",
                                recovery_attempted=["retry_3x"])
            # 4e-2. 防御性再验证：主确认点击前输入框必须已写入（为空走上重试/抛错）
            await _ensure_input_written()
            await main_btn.click(timeout=15000)
        except StepError:
            raise
        except Exception as exc:
            raise StepError(4, "selector_not_found", f"主 Confirmar 点击失败: {exc}",
                            recovery_attempted=["retry_3x"]) from exc
        await asyncio.sleep(5)
        # 提交后应跳转 hub-v2；未跳转说明预约未提交
        try:
            await self.browser.page.wait_for_url("**/hub-v2**", timeout=15000)
        except Exception:
            raise StepError(4, "business", "预约提交后未跳转 hub-v2，预约可能未提交",
                            recovery_attempted=["check_appointment_state"])
        self._mark_done(4)
        if self.state.record_id:
            self.feishu.send_message(f"✅ 步骤4完成: #{self.state.shipment_id} 已预约")

    async def _pick_date(self) -> str:
        """灰圈算法：找 div.day--current，从其后的第 31 格起选日期（跳过表头与 disabled 格）。

        规则：所选预约日期必须 >= 今天+31 天；若 +31 天那天 disabled，则继续向后延一天。
        主路径（有 day--current）：target = idx + 31，跳过表头字母格与 day--disabled 格；
        fallback（无 day--current）：精确计算今天+31 天目标日期，按日历月份文本匹配选中
        （或其后第一个可用日）；两种路径点击后都验证 day--selected 生效（轮询最多 3s）。
        """
        day_sel = "div.day"
        days = self.browser.page.locator("div.day")
        for flip in range(2):
            # 直接用 page.evaluate 找 div.day--current 在所有 div.day 中的索引
            idx = await self.browser.page.evaluate(
                """() => {
                    const days = document.querySelectorAll('div.day');
                    for (let i = 0; i < days.length; i++) {
                        if (days[i].classList.contains('day--current')) return i;
                    }
                    return -1;
                }"""
            )
            if idx < 0:
                # fallback：无 div.day--current（pitfall #123）——
                # 精确计算今天+31 天的目标日期并选中（或其后第一个可用日），不再选视图内最后一天
                res = await self.browser.page.evaluate(
                    """() => {
                        const days = document.querySelectorAll('div.day');
                        const n = days.length;
                        const now = new Date();
                        const target = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 31);
                        const MONTHS = ['enero','febrero','marzo','abril','mayo','junio','julio',
                                       'agosto','septiembre','octubre','noviembre','diciembre'];
                        // 探测日历月份文本（focus-ui datepicker）
                        const my = document.querySelector('.focus-ui-datepicker__content__month-year');
                        const headerText = my ? (my.textContent || '').trim().toLowerCase() : '';
                        const headerReliable = MONTHS.some(m => headerText.includes(m)) && /\\d{4}/.test(headerText);
                        if (headerReliable) {
                            // 解析视图月份序列（可能双月 "agosto - septiembre"）
                            const viewMonths = MONTHS.map((m, mi) => headerText.includes(m) ? mi : -1)
                                                        .filter(x => x >= 0);
                            const ym = headerText.match(/(\\d{4})/);
                            const viewYear = ym ? parseInt(ym[1], 10) : now.getFullYear();
                            // 找所有 "1" 的位置作为月份边界
                            const ones = [];
                            for (let i = 0; i < n; i++) {
                                const t = (days[i].textContent || '').trim();
                                if (/^\\d+$/.test(t) && parseInt(t, 10) === 1) ones.push(i);
                            }
                            const tMonth = target.getMonth();
                            const tIdxInView = viewMonths.indexOf(tMonth);
                            if (tIdxInView >= 0 && target.getFullYear() === viewYear) {
                                const start = ones[Math.min(tIdxInView, ones.length - 1)] ?? 0;
                                const end = tIdxInView + 1 < ones.length ? ones[tIdxInView + 1] : n;
                                // 目标日当天或其后第一个可用日（同月区间内向后延）
                                for (let i = start; i < end; i++) {
                                    const t = (days[i].textContent || '').trim();
                                    if (/^\\d+$/.test(t) && parseInt(t, 10) >= target.getDate()
                                            && !days[i].classList.contains('day--disabled')) {
                                        return {found: true, idx: i, mode: 'header', header: headerText};
                                    }
                                }
                                // 当月视图内没有 >= 目标日的可用日 → 需翻月
                                return {found: false, needFlip: true, mode: 'header', header: headerText};
                            }
                            // 目标月不在当前视图 → 需翻月
                            return {found: false, needFlip: true, mode: 'header', header: headerText};
                        }
                        // header 不可靠：降级为「目标日数字 + 视图内该数字后的第一个可用格」
                        for (let i = 0; i < n; i++) {
                            const t = (days[i].textContent || '').trim();
                            if (/^\\d+$/.test(t) && parseInt(t, 10) === target.getDate()) {
                                for (let j = i; j < n; j++) {
                                    const t2 = (days[j].textContent || '').trim();
                                    if (/^\\d+$/.test(t2) && !days[j].classList.contains('day--disabled')) {
                                        return {found: true, idx: j, mode: 'approx', header: headerText};
                                    }
                                }
                                break;
                            }
                        }
                        return {found: false, needFlip: true, mode: 'approx', header: headerText};
                    }"""
                )
                if not res.get("found"):
                    # 当前视图找不到目标日：flip==0 翻月重试；flip==1 抛错
                    if flip == 0:
                        await self._flip_month()
                        continue
                    raise StepError(4, "selector_not_found",
                                    f"日期选择失败：fallback 翻月后仍找不到 >= 今天+31 的可用日 "
                                    f"(header={res.get('header')!r})",
                                    recovery_attempted=["gray_circle_fallback", "gray_circle_skip_disabled"])
                self._log(f"  ⚠️ 未找到 div.day--current，fallback 按目标日期 "
                          f"{await self._target_date_str()} 选中 (idx={res['idx']}, mode={res['mode']}, "
                          f"header={res.get('header')!r})")
                txt = (await days.nth(res["idx"]).text_content() or "").strip()
                await days.nth(res["idx"]).click(timeout=15000)
                await asyncio.sleep(1.0)
                await self._verify_day_selected(days, res["idx"])
                return txt
            n = await days.count()
            target = idx + 31  # 确保至少31天后（表头占7格，+30可能不够）
            txt = ""
            # 跳过表头（纯字母格）与 disabled 格（合并进同一循环）
            while target < n:
                cls = await days.nth(target).get_attribute("class") or ""
                txt = (await days.nth(target).text_content() or "").strip()
                if re.match(r"^[A-Z]+$", txt) or "day--disabled" in cls:
                    target += 1
                    continue
                break
            if target >= n:
                if flip == 0:
                    await self._flip_month()
                    continue
                raise StepError(4, "selector_not_found", "日期选择失败：翻月后仍无法选中",
                                recovery_attempted=["gray_circle_skip_disabled", "gray_circle_flip_2"])
            await days.nth(target).click(timeout=15000)
            await asyncio.sleep(1.0)
            await self._verify_day_selected(days, target)
            return txt
        raise StepError(4, "selector_not_found", "日期选择失败：翻月后仍无法选中",
                        recovery_attempted=["gray_circle_skip_disabled", "gray_circle_flip_2"])

    async def _target_date_str(self) -> str:
        """fallback 目标日期（今天+31）字符串，仅用于日志。"""
        try:
            return await self.browser.page.evaluate(
                """() => {
                    const now = new Date();
                    const t = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 31);
                    return t.toISOString().slice(0, 10);
                }"""
            )
        except Exception:
            return "today+31"

    async def _verify_day_selected(self, days, idx: int) -> None:
        """点击日期格后验证 day--selected 生效（轮询最多 3s），未生效重试点击一次，仍失败抛错。"""
        for attempt in range(2):
            for _ in range(6):  # 6 * 0.5s = 3s
                cls = await days.nth(idx).get_attribute("class") or ""
                if "day--selected" in cls:
                    return
                await asyncio.sleep(0.5)
            if attempt == 0:
                self._log("  ⚠️ day--selected 未生效，重试点击日期格")
                await days.nth(idx).click(timeout=15000)
                await asyncio.sleep(1.0)
        raise StepError(4, "selector_not_found", "日期选择失败：点击后 day--selected 未生效",
                        recovery_attempted=["day_selected_retry"])

    async def _appointment_input_value(self) -> str:
        """读取预约日期时间只读输入框当前值（与 4c 点击打开的输入框同一元素）。"""
        try:
            return await self.browser.page.evaluate(
                """() => {
                    const el = document.querySelector('input[readonly]');
                    return el ? (el.value || '') : '';
                }"""
            )
        except Exception:
            return ""

    async def _wait_appointment_input_written(self, timeout: float = 6.0) -> bool:
        """轮询日期时间只读输入框，等待日历区 Confirmar 写入日期+时间。

        判定：值非空且含 "HH:MM" 时间文本（如 "7 de septiembre, 2:00 hs"）。
        每 0.5s 检查一次，最长 timeout 秒；未写入返回 False。
        """
        deadline = time.monotonic() + timeout
        while True:
            val = await self._appointment_input_value()
            if val and re.search(r"\d{1,2}:\d{2}", val):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def _dismiss_overlay(self) -> None:
        """关闭页面教程/广告蒙层（coachmarks + ML 广告/推广弹窗）。"""
        try:
            # 1) ML 教程蒙层（coachmarks）：aria-label="Close" + class=andes-coach-marks
            close_btn = self.browser.page.locator(
                '[aria-label="Close"], .andes-coach-marks__step__close-button, '
                '#coachmarks-fast-shipping-hero-step-close-button'
            ).first
            if await close_btn.count() > 0 and await close_btn.is_visible():
                await close_btn.click(force=True)
                await asyncio.sleep(0.5)
                self._log("  已关闭教程蒙层")
        except Exception:
            pass
        try:
            # 2) 广告/推广弹窗（andes-modal / role=dialog 内的关闭按钮）
            modal_close = self.browser.page.locator(
                '.andes-modal [aria-label="Close"], .andes-modal [aria-label="Cerrar"], '
                '[role="dialog"] [aria-label="Close"], [role="dialog"] [aria-label="Cerrar"], '
                '.andes-modal__close, .andes-modal__header__close, '
                '[role="dialog"] button[class*="close"]'
            ).first
            if await modal_close.count() > 0 and await modal_close.is_visible():
                await modal_close.click(force=True)
                await asyncio.sleep(0.5)
                self._log("  已关闭广告/推广弹窗")
        except Exception:
            pass

    async def _flip_month(self) -> None:
        """点击日历下月按钮（fallback 链）。"""
        nm_chain = self.sel.chain(4, "next_month")
        for sel in nm_chain:
            loc = self.browser.page.locator(sel)
            try:
                if await loc.count() == 0:
                    continue
                await loc.first.click(timeout=10000, force=True)
                await asyncio.sleep(1.5)
                return
            except Exception:
                continue
        raise StepError(4, "selector_not_found", "翻月按钮不可用",
                        recovery_attempted=["next_month"])

    # ==================================================
    # 步骤 5：包装确认（写操作）
    # ==================================================
    async def step5_package_confirm(self) -> None:
        self._log("步骤5 包装确认")
        await self.browser.visit_plan_page("procedures", self.state.shipment_id, step=5)
        await asyncio.sleep(3)
        # 勾选全部 checkbox（原生 click，React onChange 自动触发）
        r = await self.browser.click_checkboxes_with_fallback(5, "checkboxes")
        if not r.startswith("checked"):
            raise StepError(5, "business", f"包装确认：未找到 checkbox ({r})",
                            recovery_attempted=["checkbox_mouseevent"])
        await self.browser.click_with_fallback(5, "confirm_btn", "Confirmar")
        await asyncio.sleep(3)
        # 提交后应跳转 hub-v2；未跳转说明包装确认未提交
        try:
            await self.browser.page.wait_for_url("**/hub-v2**", timeout=15000)
        except Exception:
            raise StepError(5, "business", "包装确认后未跳转 hub-v2，确认可能未提交",
                            recovery_attempted=["check_procedures_state"])
        self._mark_done(5)
        if self.state.record_id:
            self.feishu.send_message(f"✅ 步骤5完成: 包装确认 - {self.state.sku}")

    # ==================================================
    # 步骤 6：标签下载（只读下载）
    # ==================================================
    async def step6_labels(self, dry_run: bool = False) -> None:
        self._log("步骤6 标签下载")
        # dry_run 且无货件号：没有可下载标签的货件，直接记为跳过（避免访问不存在的页面）
        if dry_run and not self.state.shipment_id:
            self.state.dry_run_notes.append(
                "步骤6 跳过：dry_run 无货件号（货件由写步骤 3 创建）")
            self._log("  dry_run 无货件号，步骤6 跳过")
            return
        await self.browser.visit_plan_page("labeling", self.state.shipment_id, step=6)
        await asyncio.sleep(3)
        # 提取 ML 码（产品标页面有完整产品信息）
        try:
            self.state.ml_code = await self.browser.evaluate(JS_EXTRACT_ML_CODE, step=6)
        except StepError:
            pass
        self._log(f"  ML码: {self.state.ml_code}")
        # 6-1. 勾选所有 checkbox（原生 click，替代 fiber onChange hack）
        r = await self.browser.click_checkboxes_with_fallback(6, "checkboxes", wait_after=2.0)
        if not r.startswith("checked"):
            raise StepError(6, "selector_not_found", f"勾选产品 checkbox 失败 ({r})",
                            recovery_attempted=["fiber_onChange", "mouse_event"])
        self._log(f"  复选框: {r}")
        if dry_run:
            self.state.dry_run_notes.append("步骤6 只做勾选探测（dry_run 不触发下载）")
            self._mark_done(6)
            return
        # 6-2. 等按钮启用（React 受控组件：勾选后需渲染才能启用 Descargar etiquetas）
        await self.browser.wait_for_text("Descargar etiquetas", selector="button", timeout=15, action="descargar_btn_enabled")
        await self.browser.click_with_fallback(6, "descargar_btn", "Descargar etiquetas",
                                               wait_after=2.0)
        # 6-3. 弹窗「¿Cómo quieres descargar tus etiquetas?」→ 点「Descargar」
        r = await self.browser.click_dialog_button("Descargar", step=6, wait_after=3.0)
        if r != "downloaded":
            raise StepError(6, "selector_not_found", f"弹窗 Descargar 未找到 ({r})",
                            recovery_attempted=["role_dialog"])
        # 6-4. Confirmar 完成标签步骤
        await self.browser.click_with_fallback(6, "confirm_btn", "Confirmar", wait_after=3.0)
        # 上传产品标签（下载前快照，确保捕获任何文件名）
        store_tag = self._safe_name(self.state.store_name or self.cfg.store_name)
        await self._download_and_upload(
            "产品标签",
            f"产品标+{self.state.sku}+{self.state.ml_code}+{self._safe_name(self.state.name)}+{store_tag}.pdf",
            pattern="*Etiquetas-de-producto*.pdf",
            exclude=None,)
        self._mark_done(6)
        if self.state.record_id:
            self.feishu.send_message("✅ 步骤6完成: 产品标签已上传")

    # ==================================================
    # 步骤 7：打印箱唛（写操作）
    # ==================================================
    async def step7_box_labels(self) -> None:
        self._log("步骤7 打印箱唛")
        await self.browser.visit_plan_page("volumes", self.state.shipment_id, step=7)
        await asyncio.sleep(3)
        # 7a. 填箱数（原生 fill）
        await self.browser.fill_with_fallback(7, "qty_input", self.state.box, wait_after=1.0)
        # 7b. 只勾选 Andes checkbox #2/#3（Pallets 选项），跳过 #1（bultos）
        r = await self.browser.click_checkboxes_with_fallback(
            7, "andes_checkbox", nth_start=1, nth_end=3, wait_after=1.0)
        if r.startswith("need3") or not r.startswith("checked"):
            raise StepError(7, "selector_not_found", f"Andes checkbox 不足 3 个 ({r})",
                            recovery_attempted=["pointer_event"])
        # 7c. Generar etiquetas
        await self.browser.click_with_fallback(7, "generate_btn", "Generar etiquetas",
                                               wait_after=5.0)
        # 7d. 等 Descarga todas 按钮出现（箱唛生成需要时间）
        await self.browser.wait_for_text("Descarga todas", selector="button", timeout=30, action="download_all_btn")
        await self.browser.click_contains_with_fallback(7, "download_all", "Descarga todas",
                                                        wait_after=2.0)
        # 7e. 弹窗「Descarga e imprime las etiquetas」→ Normal → Descargar etiquetas
        r = await self.browser.click_modal_normal_and_button(
            "Descarga e imprime", "Descargar etiquetas", step=7, wait_after=3.0)
        if r != "downloaded":
            raise StepError(7, "selector_not_found", f"箱唛弹窗下载失败 ({r})",
                            recovery_attempted=["role_dialog", "leaf_normal"])
        # 7f. 勾选 fragile checkbox（容器原生点击）
        r = await self.browser.click_checkboxes(
            '[data-testid="checkbox-fragils-consolidation"]', step=7, wait_after=1.0)
        if not r.startswith("checked"):
            raise StepError(7, "selector_not_found", f"未找到 fragile checkbox ({r})",
                            recovery_attempted=["fiber_onChange"])
        # 7g. 等加载旋转层消失再点 Continuar（箱唛生成后约5秒loading）
        await asyncio.sleep(5)
        await self.browser.click_with_fallback(7, "continuar_btn", "Continuar", wait_after=3.0)
        # 上传箱唛（下载前快照，确保捕获任何文件名）
        store_tag = self._safe_name(self.state.store_name or self.cfg.store_name)
        await self._download_and_upload(
            "箱唛",
            f"{self.state.shipment_id}+{self.state.box}箱+{store_tag}.pdf",
            pattern="Envio-*-Etiquetas-de-bultos.pdf")
        self._mark_done(7)
        if self.state.record_id:
            self.feishu.send_message("✅ 步骤7完成: 箱唛已上传")

    # ==================================================
    # 步骤 8：取消预约（写操作）
    # ==================================================
    async def step8_cancel_appointment(self) -> None:
        self._log("步骤8 取消预约")
        # 直接导航到 appointment-v2 页面（跳过从列表找 Editar——列表有虚拟滚动，Editar 可能没渲染）
        await self.browser.visit_plan_page("appointment-v2", self.state.shipment_id, step=8)
        await asyncio.sleep(3)
        # 滚动到底部（Cancelar reserva 在页面下方）
        await self.browser.page.evaluate("window.scrollTo(0, 1500)")
        await asyncio.sleep(0.5)
        # 点击 Cancelar reserva
        await self.browser.click_with_fallback(8, "cancelar_reserva", "Cancelar reserva")
        await asyncio.sleep(1)
        await self.browser.click_with_fallback(8, "cancelar_cita", "Cancelar cita")
        await asyncio.sleep(2)
        self._mark_done(8)

    # ==================================================
    # 文件处理：自动重命名 + 上传飞书 Base
    # ==================================================
    @staticmethod
    def _safe_name(name: str) -> str:
        return re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip() or "产品"

    def _snapshot_download_dir(self) -> tuple[Path, set[str]]:
        """返回下载目录和当前 PDF 文件集合。目录来自 _open_store() 的 downloadFolderPath。"""
        dl_dir = self._dl_dir or Path(self.cfg.ziniaodl)  # 兜底：未开店时退回配置目录
        if dl_dir.exists():
            return dl_dir, {p.name for p in dl_dir.glob("*.pdf")}
        return dl_dir, set()

    def _find_new_pdf(self, dl_dir: Path, before: set[str], exclude: Optional[str] = "") -> Optional[Path]:
        """比较下载目录，返回新增的 PDF（跳过已处理文件）。"""
        if not dl_dir.exists():
            return None
        after = {p.name: p for p in dl_dir.glob("*.pdf")}
        new_names = set(after.keys()) - before
        for name in sorted(new_names):
            if exclude and re.search(exclude, name):
                continue
            # 跳过已处理文件（已重命名）
            if "产品标" in name or "箱" in name:
                continue
            if "listado" in name.lower() or "Descargar" in name or "preparation" in name.lower():
                continue
            return after[name]
        return None

    async def _download_and_upload(self, field_name: str, rename_to: str,
                             pattern: str = "*.pdf",
                             exclude: Optional[str] = "",
                             download_trigger: Optional[Callable[[], Any]] = None) -> bool:
        """通用下载+上传：下载前快照→触发下载→等待→找新PDF→重命名→上传。
        
        返回 True 表示成功上传。download_trigger 为 None 时使用 pattern 匹配兜底。
        """
        dl_dir, before = self._snapshot_download_dir()
        if download_trigger:
            download_trigger()
            await asyncio.sleep(5)  # 等待下载完成
        
        # 优先找新增文件
        new_pdf = self._find_new_pdf(dl_dir, before, exclude)
        if new_pdf:
            src = new_pdf
        else:
            # 兜底：pattern 匹配
            candidates = sorted(dl_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                # 终极兜底：找最新非排除PDF
                all_pdfs = sorted(dl_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
                for pdf in all_pdfs:
                    if exclude and re.search(exclude, pdf.name):
                        continue
                    if "产品标" in pdf.name or "箱" in pdf.name or "Etiquetas-de-bultos" in pdf.name:
                        continue
                    candidates = [pdf]
                    break
            if not candidates:
                self._log(f"  ⚠️ 未找到 {field_name} PDF，跳过上传")
                return False
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
            return True
        else:
            self._log(f"  文件已重命名（未上传）: {renamed.name}")
            return False

    def _open_store(self) -> tuple[Path, Optional[int]]:
        """切换到目标店铺（可见窗口，每店独立CDP端口）。不关店，频繁关店可能触发ML安全检测。

        店铺识别：优先本地 stores.json（环境名 key = 飞书「店铺名称」选项）；
        stores.json 缺失时回退内置 STORE_MAP（开发测试用，日志标注 dev 模式）。
        """
        store_name = self.state.store_name or self.cfg.store_name
        if not store_name:
            raise StepError(1, "config_error",
                            "未指定店铺名称，请运行初始化向导（argent skill setup ml-fulfillment）")
        store_info, dev_mode = _resolve_store(store_name)
        if not store_info:
            raise StepError(1, "config_error",
                            f"未找到店铺 {store_name} 的紫鸟环境，请运行初始化向导（argent skill setup ml-fulfillment）")
        if dev_mode:
            self._log("  ⚠️ stores.json 缺失，使用内置 STORE_MAP（开发测试用，客户环境由 stores.json 提供）")
        # 紫鸟环境名：stores.json 的 key 即环境名；STORE_MAP 的 name 即环境名
        env_name = store_info.get("name") or store_name
        # 打开目标店铺
        try:
            result = subprocess.run(
                ["ziniao-cli", "store", "open", "--name", env_name],
                capture_output=True, text=True, timeout=60,
            )
            data = json.loads(result.stdout)
            dl_path = data["data"]["downloadFolderPath"]
            reused = data.get("data", {}).get("reused", False)
        except Exception as exc:
            raise StepError(1, "cli_error",
                            f"ziniao-cli store open 失败（店铺 {env_name}）: {exc}") from exc
        # 发现目标店铺的 CDP 端口
        port = self._find_store_port(env_name, reused)
        self._log(f"  店铺: {env_name} (storeId={store_info['store_id']})")
        self._log(f"  下载目录: {dl_path}")
        return Path(dl_path), port

    def _build_port_map(self) -> dict[str, int]:
        """启动时建立环境名→CDP端口映射。

        识别映射来自 stores.json（环境名 key → cdp_port，初始化向导写入）；
        端口探测（lsof）保留，仅作 stores.json 缺失/无 cdp_port 时的兜底发现。
        不再从页面提取用户标识匹配（移除 USER_PATTERNS）。
        """
        port_map: dict[str, int] = {}
        for env_name, info in _load_stores().items():
            port = info.get("cdp_port")
            if port:
                port_map[env_name] = int(port)
        if port_map:
            return port_map
        # 兜底：stores.json 无 cdp_port（或缺失）时用端口探测，单端口归唯一店铺
        ports = _discover_cdp_ports()
        stores = _load_stores()
        if len(ports) == 1 and stores:
            return {next(iter(stores)): ports[0]}
        return {}

    def _find_store_port(self, store_name: str, reused: bool) -> Optional[int]:
        """找到目标店铺的CDP端口。

        优先 stores.json 的 cdp_port（环境名 key）；stores.json 存在但无该店铺 →
        报错提示初始化向导；缺失/无 cdp_port 时回退端口探测（单端口场景）。
        """
        stores = _load_stores()
        if stores:
            info = stores.get(store_name)
            if not info:
                raise StepError(1, "config_error",
                                f"未找到店铺 {store_name} 的紫鸟环境，请运行初始化向导（argent skill setup ml-fulfillment）")
            if info.get("cdp_port"):
                return int(info["cdp_port"])
        if not hasattr(self, '_port_map'):
            self._port_map = self._build_port_map()
        if store_name in self._port_map:
            return self._port_map[store_name]
        # 兜底：单端口场景（端口发现）
        ports = _discover_cdp_ports()
        return ports[0] if ports else None

    # ---- 店铺级 flock 互斥锁 ----
    def _resolve_store_id(self) -> str:
        """解析店铺级锁的 storeId：环境名 → stores.json；未指定店铺时 cfg.store_id；找不到报错。"""
        store_name = self.state.store_name or self.cfg.store_name
        if store_name:
            info, dev_mode = _resolve_store(store_name)
            if not info:
                raise StepError(0, "config_error",
                                f"未找到店铺 {store_name} 的紫鸟环境，请运行初始化向导（argent skill setup ml-fulfillment）")
            if dev_mode:
                self._log("  ⚠️ stores.json 缺失，使用内置 STORE_MAP（开发测试用，客户环境由 stores.json 提供）")
            return str(info.get("store_id") or "")
        if self.cfg.store_id:
            return self.cfg.store_id
        raise StepError(0, "config_error",
                        "未找到店铺的紫鸟环境，请运行初始化向导（argent skill setup ml-fulfillment）")

    def _acquire_store_lock(self) -> Optional[Any]:
        """非阻塞获取店铺级 flock 互斥锁（/tmp/ziniao-<storeId>.lock）。

        同店并发（cron 多轮重叠 / 重复 spawn）时后到进程拿不到锁 → 返回 None，
        调用方输出 {"status":"skipped","reason":"store_busy"} 后以退出码 0 退出（下轮重试）。
        锁随进程退出自动释放；fd 引用保存在 self._store_lock 防止被提前回收。
        """
        import fcntl
        lock_path = f"/tmp/ziniao-{self._resolve_store_id()}.lock"
        f = open(lock_path, "a+")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return None
        return f

    # ==================================================
    # 主流程
    # ==================================================
    async def run(self, mode: str, allow_write: bool, step_filter: Optional[int],
                  record_id: Optional[str], sku: Optional[str], qty: Optional[str],
                  box: Optional[str], shipment_id: Optional[str],
                  store_name: Optional[str] = None) -> dict[str, Any]:
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
            if store_name:
                # 指定店铺：取第一条店铺名匹配的记录（空店铺名历史记录不在此列）
                rec = next((r for r in pending if r.get("store_name") == store_name), None)
                if rec is None:
                    self._log(f"没有 {store_name} 的待处理 Pending 记录，本轮退出。")
            else:
                rec = pending[0] if pending else None
                if rec is None:
                    self._log("没有待处理的 Pending 记录，本轮退出。")
            if rec is None:
                return {"status": "no_pending", "record_id": None, "shipment_id": None,
                        "sku": None, "completed_steps": [], "failed_step": None,
                        "error": None, "files_uploaded": {}, "dry_run": mode == "dry-run",
                        "write_steps": list(WRITE_STEPS)}
            self.state.record_id = rec["record_id"]
            self.state.sku = rec["sku"]
            self.state.name = rec["name"]
            self.state.qty = rec["qty"]
            self.state.box = rec["box"]
            self.state.store_name = rec.get("store_name", "") or self.cfg.store_name
            # 若记录已有货件号（上次部分完成），跳过步骤 3 继续执行
            existing_shipment = rec.get("shipment_id", "")
            if existing_shipment and existing_shipment not in ("", "None", "null"):
                self.state.shipment_id = existing_shipment
                self._log(f"  已有货件号 {existing_shipment}，从步骤4继续")
        if shipment_id:
            self.state.shipment_id = shipment_id
        self._log(f"🚀 处理: {self.state.sku} {self.state.name} "
                  f"{self.state.qty}件 {self.state.box}箱 (record={self.state.record_id})")

        # 店铺级 flock 互斥锁：确定店铺名之后、打开浏览器之前获取（进程退出自动释放）
        self._store_lock = self._acquire_store_lock()
        if self._store_lock is None:
            self._log("店铺忙（其他进程持有 flock 锁），本轮跳过。")
            return {"status": "skipped", "reason": "store_busy"}

        dry_run = mode == "dry-run"
        # 启动标记（状态=运行中）必须与打开店铺在同一 protected 区域：后续任何异常
        # （StepError 或 cli_error 等非 StepError）都走统一失败分支（状态=失败/就绪=false），
        # 防止「运行中+就绪=true」残留记录死锁。dry_run 模式不写飞书 Base（保持零副作用）
        try:
            if self.state.record_id and not dry_run:
                self.feishu.update_field(self.state.record_id, "状态", "运行中")
                self.feishu.update_step(self.state.record_id, "步骤1：打开店铺")

            # 根据店铺名称启动紫鸟店铺（仅一次），获取下载目录；随后 CDP 接管浏览器
            self._dl_dir, port = self._open_store()
            if not port:
                raise StepError(1, "cli_error", "CDP 端口未发现（浏览器窗口可能未就绪）")
            self.browser.cdp_url = f"http://127.0.0.1:{port}"
            self._log(f"  CDP 端口: {self.browser.cdp_url}")
            await self.browser.connect(download_dir=str(self._dl_dir))
        except StepError as exc:
            self._fail_record(step_filter or 1, exc, dry_run=dry_run)
            return self._result("failed", failed_step=step_filter or 1, error=exc.to_dict(),
                                step_summaries={}, dry_run=dry_run)
        except Exception as exc:  # 环境类异常（非 StepError，如 playwright 不可用）→ 统一失败分支
            env_exc = StepError(step_filter or 1, "cli_error", f"环境异常: {exc}",
                                recovery_attempted=["env_error"])
            self._log(f"❌ 环境异常（步骤{step_filter or 1}）: {exc}")
            self._fail_record(step_filter or 1, env_exc, dry_run=dry_run)
            return self._result("failed", failed_step=step_filter or 1, error=env_exc.to_dict(),
                                step_summaries={}, dry_run=dry_run)

        steps = [step_filter] if step_filter else list(range(1, 9))
        step_summaries: dict[int, Any] = {}

        for step in steps:
            try:
                # 写步骤保护（需在 try 内，needs_approval 要走统一结果通道）
                if step in WRITE_STEPS:
                    if not self._guard_write(step, allow_write, dry_run):
                        continue
                if step == 1:
                    s1 = await self.step1_prepare()
                    if isinstance(s1, dict) and s1.get("status") == "already_completed":
                        self._log(f"  货件已完成，跳过后续步骤")
                        return self._result("success", failed_step=None, error=None,
                                            step_summaries={1: s1}, dry_run=dry_run)
                    step_summaries[1] = s1
                elif step == 2:
                    await self.step2_entry()
                elif step == 3:
                    if self.state.shipment_id:
                        self._log(f"  跳过步骤3（已有货件号 {self.state.shipment_id}）")
                        self._mark_done(3)
                    else:
                        await self.step3_select_product()
                elif step == 4:
                    await self.step4_appointment()
                elif step == 5:
                    await self.step5_package_confirm()
                elif step == 6:
                    await self.step6_labels(dry_run=dry_run)
                elif step == 7:
                    await self.step7_box_labels()
                elif step == 8:
                    await self.step8_cancel_appointment()
            except StepError as exc:
                if exc.err_type == "needs_approval":
                    return self._result("needs_approval", failed_step=step,
                                        error=exc.to_dict(), step_summaries=step_summaries,
                                        dry_run=dry_run)
                self._log(f"❌ 步骤{step} 失败: {exc.message}")
                screenshot = await self._screenshot_on_error(step)
                page_url, dom_summary = await self._capture_page_diag()
                self._record_failure(step, exc, screenshot=screenshot,
                                     page_url=page_url, dom_summary=dom_summary)
                self._report_failure(step, exc, screenshot=screenshot,
                                     page_url=page_url, dom_summary=dom_summary)
                auto_retry: Optional[int] = None
                if self.state.record_id:
                    self.feishu.update_field(self.state.record_id, "状态", "失败")
                    self.feishu.update_field(self.state.record_id, "就绪", False)
                    self.feishu.update_step(self.state.record_id, f"失败：{exc.message}")
                    # 层2兜底：网络类错误自动重置（保留货件号续传，最多 2 次）→ 状态改回 Pending
                    auto_retry = self._maybe_auto_reset(step, exc, page_url=page_url,
                                                        dom_summary=dom_summary)
                    if auto_retry is None:
                        self.feishu.send_message(self._friendly_failure_message())
                status = "failed" if not self.state.completed_steps else "partial"
                result = self._result(status, failed_step=step, error=exc.to_dict(),
                                      step_summaries=step_summaries, dry_run=dry_run)
                if auto_retry is not None:
                    result["auto_retry"] = auto_retry  # 已自动重置：下轮 cron 续传
                return result
            # 每步完成后同步 Base 当前步骤（dry_run 不写）
            if self.state.record_id and step < 8 and not dry_run:
                self.feishu.update_step(self.state.record_id, f"步骤{step + 1}：{STEP_NAMES[step + 1]}")

        # ── 全部完成 ──
        # dry_run 不写飞书（保持零副作用——否则会把 Pending 记录标记为已完成/取消就绪）
        if self.state.record_id and not dry_run:
            self.feishu.update_field(self.state.record_id, "状态", "已完成")
            self.feishu.update_step(self.state.record_id, "全部完成")
            self.feishu.update_field(self.state.record_id, "就绪", False)
            self.feishu.send_message(
                f"🎉 FULL 货件完成！\nSKU: {self.state.sku} {self.state.name}\n"
                f"货件: #{self.state.shipment_id}\n数量: {self.state.qty}件 / {self.state.box}箱\n"
                f"文件已上传到飞书多维表格")
        await self.browser.close()
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
        description="FULL 货件编排器：Playwright CDP + lark-cli 封装，返回 JSON 给 Agent。")
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
    p.add_argument("--store-id", help="覆盖店铺 ID（兼容保留，CDP 模式不再使用）")
    p.add_argument("--store-name", help="店铺名过滤待处理记录（如 1店-子账号；取第一条店铺名匹配的 Pending+就绪 记录）")
    p.add_argument("--cdp-url", help=f"紫鸟浏览器 CDP 地址（默认 {DEFAULT_CDP_URL}）")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    if args.store_id:
        cfg.store_id = args.store_id
    if args.store_name:
        cfg.store_name = args.store_name
    if args.cdp_url:
        cfg.cdp_url = args.cdp_url

    # inspect：无副作用自检
    if args.mode == "inspect":
        try:
            sel = Selectors()
        except StepError as exc:
            print(json.dumps({"status": "failed", "error": exc.to_dict()},
                             ensure_ascii=False))
            return 1
        stores = _load_stores()
        result = {
            "status": "ok",
            "fulfillment_js": str(FULFILLMENT_JS),
            "selectors": sel.summary(),
            "config": {
                "feishu_ready": cfg.feishu_ready,
                "browser_ready": cfg.browser_ready,
                "cdp_url": cfg.cdp_url,
                "env_file": str(_default_env_file()),
            },
            "stores": {
                "source": "stores.json" if stores else "STORE_MAP(dev)",
                "json_path": str(STORES_JSON),
                "stores": stores or {k: {"name": v["name"], "store_id": v["store_id"]}
                                     for k, v in STORE_MAP.items()},
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
        result = asyncio.run(orch.run(mode=args.mode, allow_write=args.allow_write,
                                      step_filter=args.step, record_id=args.record_id,
                                      sku=args.sku, qty=args.qty, box=args.box,
                                      shipment_id=args.shipment_id,
                                      store_name=args.store_name))
    except StepError as exc:  # 顶层兜底（如配置缺失/步骤保护未捕获）
        orch._fail_record(exc.step or 0, exc, dry_run=args.mode == "dry-run")
        result = {"status": "failed", "record_id": orch.state.record_id or None,
                  "shipment_id": orch.state.shipment_id or None,
                  "sku": orch.state.sku or None,
                  "completed_steps": orch.state.completed_steps,
                  "failed_step": exc.step or None,
                  "error": exc.to_dict(), "files_uploaded": orch.state.files_uploaded}
    except Exception as exc:  # 环境类异常兜底（非 StepError，如 playwright 不可用）→ 统一失败分支防死锁
        env_exc = StepError(0, "cli_error", f"环境异常: {exc}",
                            recovery_attempted=["env_error"])
        orch._log(f"❌ 顶层环境异常: {exc}")
        orch._fail_record(0, env_exc, dry_run=args.mode == "dry-run")
        result = {"status": "failed", "record_id": orch.state.record_id or None,
                  "shipment_id": orch.state.shipment_id or None,
                  "sku": orch.state.sku or None,
                  "completed_steps": orch.state.completed_steps,
                  "failed_step": 0,
                  "error": env_exc.to_dict(), "files_uploaded": orch.state.files_uploaded}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in ("success", "no_pending", "needs_approval", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
