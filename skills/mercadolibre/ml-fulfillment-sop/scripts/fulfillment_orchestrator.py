#!/usr/bin/env python3
"""FULL 货件编排器 — Playwright CDP 驱动的结构化封装（替代 ziniao-cli / poll-fulfillment.sh）。

架构（与旧 ziniao-cli 的本原区别）:
    Cron 触发 wrapper (poll-fulfillment.sh, no_agent)
      → exec /tmp/pw-venv/bin/python3 fulfillment_orchestrator.py --mode full --allow-write
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
        全流程编排。写步骤默认拒绝，需 --allow-write 才执行
    /tmp/pw-venv/bin/python3 fulfillment_orchestrator.py --mode step --step N [--allow-write]
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
ML_STORE_NAME / FEISHU_USER_ID / ZINIAO_DL / ML_CDP_URL），或本地
~/.hermes/scripts/fulfillment.env。本文件不包含任何硬编码凭据。
"""

from __future__ import annotations

import argparse
import asyncio
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
DEFAULT_CDP_URL = "http://127.0.0.1:52420"

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
        cdp_url=pick("ML_CDP_URL") or DEFAULT_CDP_URL,
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

    async def connect(self, step: int = 0) -> None:
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
        # 定位 ML 页面（跨所有 context）：优先 mercadolibre.com.mx，其次 mercadolibre.com
        target, self._context = None, None
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                if "mercadolibre.com.mx" in (p.url or ""):
                    target, self._context = p, ctx
                    break
            if target:
                break
        if target is None:
            for ctx in self._browser.contexts:
                for p in ctx.pages:
                    if "mercadolibre.com" in (p.url or ""):
                        target, self._context = p, ctx
                        break
                if target:
                    break
        if target is None:
            if self._browser.contexts:
                self._context = self._browser.contexts[0]
            else:
                self._context = await self._browser.new_context()
            pages = self._context.pages
            target = pages[0] if pages else await self._context.new_page()
        try:
            await target.bring_to_front()
        except Exception:
            pass
        self._page = target
        print(f"[{time.strftime('%H:%M:%S')}] ✅ CDP 已连接 {self.cdp_url} 页面: {target.url[:90]}",
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
        try:
            await self.page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        except Exception as exc:
            # ML 长连接/轮询常导致 networkidle 超时；页面已加载则按 domcontentloaded 继续
            print(f"[{time.strftime('%H:%M:%S')}] ⚠️ networkidle 超时({url[:60]}): "
                  f"{type(exc).__name__}，改用 domcontentloaded",
                  file=sys.stderr, flush=True)
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception as exc2:
                raise StepError(step, "timeout", f"页面导航失败: {url}: {exc2}") from exc2
        # 等待页面主体容器渲染完成（避免后续点击误触导航栏）
        try:
            await self.page.wait_for_selector("main, #root-app", timeout=15000)
        except Exception:
            pass
        if wait_after:
            await asyncio.sleep(wait_after)

    async def visit_plan_page(self, path: str, shipment_id: str, step: int) -> None:
        """访问货件子页面：优先用已知货件号拼 URL，其次从当前 URL 提取 inbound ID。"""
        url = ""
        if shipment_id:
            url = f"{ML_BASE_URL}/shipping/inbounds/{shipment_id}/{path}"
        else:
            try:
                m = await self.evaluate(
                    "(function(){var m=location.href.match(/\\/inbounds\\/(\\d+)/);return m?m[1]:'';})();",
                    step=step)
            except StepError:
                m = ""
            url = (f"{ML_BASE_URL}/shipping/inbounds/{m}/{path}" if m
                   else f"{ML_BASE_URL}/shipping/{path}")
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
                    "store_name": str(f.get("店铺名称") or ""),
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
  var m = location.href.match(/\\/plans\\/(\\d+)/);
  if (m) return m[1];
  var m2 = location.href.match(/\\/(\\d{8})\\//);
  if (m2) return m2[1];
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

    async def _screenshot_on_error(self, step: int) -> None:
        """步骤失败时自动截图现场到 /tmp/fulfillment-screenshots/。"""
        try:
            d = Path("/tmp/fulfillment-screenshots")
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"step{step}-{time.strftime('%Y%m%d-%H%M%S')}.png"
            await self.browser.screenshot(str(p))
            self._log(f"  📸 失败现场截图: {p}")
        except Exception as exc:
            self._log(f"  ⚠️ 失败截图不可用: {exc}")

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
        self._log("步骤1 前期准备：连接 CDP 浏览器并检查 FULL 管理页")
        await self.browser.navigate(INBOUNDS_URL, step=1, wait_after=2.0)
        out = await self.browser.evaluate(JS_EXTRACT_SHIPMENTS, step=1)
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
        # 检查当前货件是否已完成（Reserva cancelada 等终态）
        if self.state.shipment_id:
            for s in shipments:
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
        return summary

    # ==================================================
    # 步骤 2：点击 Enviar productos 进入创建入口（只读导航）
    # ==================================================
    async def step2_entry(self) -> None:
        self._log("步骤2 货件创建入口：点击 Enviar productos")
        await self.browser.navigate(INBOUNDS_URL, step=2, wait_after=2.0)
        await self.browser.click_with_fallback(2, "enviar_btn", "Enviar productos")
        self._log("  Enviar productos 点击成功")
        # 等待导航到 Planificación 页面（轮询搜索框出现）
        sku_chain = self.sel.chain(3, "sku_input")
        await self._wait_any_selector(2, "planificacion_page", sku_chain)
        self._mark_done(2)

    # ==================================================
    # 步骤 3：选择产品与数量（写操作）
    # ==================================================
    async def step3_select_product(self) -> None:
        self._log("步骤3 选择产品与数量")
        # 3a. 搜索 SKU（fill + Enter，原生触发 React 搜索）
        await self.browser.fill_with_fallback(3, "sku_input", self.state.sku,
                                              press_enter=True, wait_after=3.0)
        self._log(f"  SKU {self.state.sku} 已搜索")
        # 提取 ML 码（非关键，失败 UNKNOWN 兜底）
        try:
            self.state.ml_code = await self.browser.evaluate(JS_EXTRACT_ML_CODE, step=3)
        except StepError:
            self.state.ml_code = "UNKNOWN"
        self._log(f"  ML码: {self.state.ml_code}")
        # 3b. 填写数量
        await self.browser.fill_with_fallback(3, "qty_input", self.state.qty, wait_after=1.0)
        # 等待按钮变为 enabled（填数量后约 3 秒）
        self._log("  等待 Continuar 按钮启用...")
        await asyncio.sleep(3.0)
        # 3c. Continuar
        await self.browser.click_with_fallback(3, "continuar_btn", "Continuar", wait_after=2.0)
        # 3d. 弹窗 Continuar con mi plan actual（fallback 链逐个等待）
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
        # 3e. 货件号（URL /plans/(\d+)/，兜底 8 位数字）
        shipment = await self.browser.evaluate(JS_EXTRACT_SHIPMENT_ID, step=3)
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
    async def step4_appointment(self) -> None:
        self._log("步骤4 货件预约时间")
        # 4a. 进入预约页（优先用货件号拼 URL，其次从 URL 提取 inbound ID）
        await self.browser.visit_plan_page("appointment-v2", self.state.shipment_id, step=4)
        await asyncio.sleep(3)
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
        # 4e. 确认（分两次：第1次点日历区 Confirmar，第2次点主确认）
        confirm_chain = self.sel.chain(4, "confirm_btn")
        r = await self.browser.click_button("Confirmar", confirm_chain[0],
                                            require_enabled=False, step=4)
        if r != "clicked":
            raise StepError(4, "selector_not_found", "未找到第1个 Confirmar（日历区）",
                            recovery_attempted=["retry_3x"])
        await asyncio.sleep(2)
        r = await self.browser.click_button("Confirmar", confirm_chain[0],
                                            require_enabled=True, step=4)
        if r != "clicked":
            raise StepError(4, "selector_not_found", "主 Confirmar 不可用或未找到",
                            recovery_attempted=["retry_3x"])
        await asyncio.sleep(5)
        self._mark_done(4)
        if self.state.record_id:
            self.feishu.send_message(f"✅ 步骤4完成: #{self.state.shipment_id} 已预约")

    async def _pick_date(self) -> str:
        """灰圈算法：找 div.day--current，从其后的第 30 格选日期（跳过表头）。

        与 poll-fulfillment.sh 验证过的算法一致；若当前视图不足则翻月重试一次。
        """
        day_sel = "div.day"
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
                raise StepError(4, "selector_not_found", "未找到 div.day--current",
                                recovery_attempted=[f"gray_circle_flip_{flip}"])
            days = self.browser.page.locator("div.day")
            n = await days.count()
            target = idx + 30
            if target >= n:
                if flip == 0:
                    await self._flip_month()
                    continue
                raise StepError(4, "selector_not_found", "日期选择失败：翻月后仍不足 30 格",
                                recovery_attempted=["gray_circle_flip_2"])
            # 跳过表头（纯字母格）
            txt = (await days.nth(target).text_content() or "").strip()
            while target < n and re.match(r"^[A-Z]+$", txt):
                target += 1
                if target >= n:
                    break
                txt = (await days.nth(target).text_content() or "").strip()
            if target >= n:
                if flip == 0:
                    await self._flip_month()
                    continue
                raise StepError(4, "selector_not_found", "日期选择失败：翻月后仍无法选中",
                                recovery_attempted=["gray_circle_flip_2"])
            await days.nth(target).click(timeout=15000)
            await asyncio.sleep(1.0)
            return txt
        raise StepError(4, "selector_not_found", "日期选择失败：翻月后仍无法选中",
                        recovery_attempted=["gray_circle_flip_2"])

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
        if self.state.ml_code == "UNKNOWN":
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
            pattern="Etiquetas-de-producto-*.pdf",
            exclude=r"Etiquetas-de-bultos|Envio-")
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
        """返回下载目录和当前 PDF 文件集合。"""
        base = Path(self.cfg.ziniaodl)
        store = self.state.store_name.strip() if self.state.store_name else ""
        if store:
            dl_dir = base.parent / store
            if not dl_dir.exists():
                dl_dir = base
        else:
            dl_dir = base
        if dl_dir.exists():
            return dl_dir, {p.name for p in dl_dir.glob("*.pdf")}
        return dl_dir, set()

    def _find_new_pdf(self, dl_dir: Path, before: set[str], exclude: str = "") -> Optional[Path]:
        """比较下载目录，返回最新增的 PDF（排除箱唛和已处理文件）。"""
        if not dl_dir.exists():
            return None
        after = {p.name: p for p in dl_dir.glob("*.pdf")}
        new_names = set(after.keys()) - before
        if not new_names:
            return None
        for name in sorted(new_names):
            pdf = after[name]
            if exclude and re.search(exclude, name):
                continue
            if "产品标" in name or "箱" in name or "Envio-" in name or "Etiquetas-de-bultos" in name:
                continue
            return pdf
        return None

    async def _download_and_upload(self, field_name: str, rename_to: str,
                             pattern: str = "*.pdf",
                             exclude: str = "",
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
                    if "产品标" in pdf.name or "箱" in pdf.name or "Envio-" in pdf.name:
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

    # ==================================================
    # 主流程
    # ==================================================
    async def run(self, mode: str, allow_write: bool, step_filter: Optional[int],
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

        dry_run = mode == "dry-run"
        # dry_run 模式不写飞书 Base（保持零副作用）
        if self.state.record_id and not dry_run:
            self.feishu.update_field(self.state.record_id, "状态", "运行中")
            self.feishu.update_step(self.state.record_id, "步骤1：打开店铺")

        # 连接浏览器（相当于旧版 store open；step 模式也可用）
        try:
            await self.browser.connect()
        except StepError as exc:
            return self._result("failed", failed_step=step_filter or 1, error=exc.to_dict(),
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
                await self._screenshot_on_error(step)
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
    p.add_argument("--store-name", help="覆盖店铺名称（兼容保留，CDP 模式不再使用）")
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
                                      shipment_id=args.shipment_id))
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
