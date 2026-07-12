"""Taobao backend driven by a persistent Playwright browser context.

Unlike an HTTP API backend, this drives a real Chromium (headed, via Xvfb on a
Linux server) so the login session survives and a human can take over the
virtual desktop (noVNC) to click the final *pay* button.

⚠️  Taobao's DOM is heavily obfuscated and changes often, and the site fights
    automation (slider captcha / risk control). The selectors below are a
    best-effort first cut and are marked ``TODO(selectors)`` — expect to
    calibrate them on the live site after your first scan-code login. Every
    method degrades gracefully: when an element can't be found it raises a
    ``PlatformError`` with the current URL (and a screenshot where useful)
    instead of crashing the server.
"""

import asyncio
import base64
import os
import re
from typing import Optional

from .base import (
    BasePlatform, PlatformError,
    LoginStatus, LoginQrcode, Item, SearchResult, ItemDetail,
    CartLine, CartView, ActionResult, OrderDraft,
)

# --- mobile emulation (H5 站更宽松) ---
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# --- URLs (TODO(selectors): 若淘宝改版在此集中调整) ---
LOGIN_QR_URL = "https://login.taobao.com/member/login.jhtml"      # 扫码登录页(PC，二维码手机扫)
MYTAOBAO_H5 = "https://h5.m.taobao.com/mlapp/mytaobao.html"       # 我的淘宝(判断登录态)
CART_H5 = "https://h5.m.taobao.com/mlapp/cart.html"               # 购物车 H5
SEARCH_H5 = "https://s.m.taobao.com/h5/search?q={q}"              # 搜索 H5
SEARCH_PC = "https://s.taobao.com/search?q={q}"
ITEM_H5 = "https://item.taobao.com/item.htm?id={id}"

# --- 风控/验证 特征(命中则提示人工 noVNC 处理) ---
_BLOCK_HINTS = ("滑块", "拖动", "验证码", "安全验证", "punish", "//sec.", "captcha", "亲，请")

# --- 反自动化检测的初始化脚本 ---
_STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

_ID_RE = re.compile(r"[?&]id=(\d+)")
_PRICE_RE = re.compile(r"(\d+(?:\.\d{1,2})?)")


class TaobaoPlatform(BasePlatform):
    def __init__(
        self,
        user_data_dir: str,
        headless: bool = False,
        site: str = "h5",
        timeout: int = 30,
        max_results: int = 10,
        max_order_amount: float = 200.0,
        max_qty: int = 10,
        slowmo: int = 0,
    ):
        self._user_data_dir = os.path.abspath(os.path.expanduser(user_data_dir))
        self._headless = headless
        self._site = site if site in ("h5", "pc") else "h5"
        self._timeout_ms = max(5, timeout) * 1000
        self._max_results = max(1, max_results)
        self._max_order_amount = max(0.0, max_order_amount)
        self._max_qty = max(1, max_qty)
        self._slowmo = max(0, slowmo)

        self._pw = None
        self._context = None
        self._page = None
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def platform_name(self) -> str:
        return "taobao"

    def is_available(self) -> bool:
        """playwright 可导入 且 profile 目录可创建。不启动浏览器、不联网。"""
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
        try:
            os.makedirs(self._user_data_dir, exist_ok=True)
            return os.access(self._user_data_dir, os.W_OK)
        except OSError:
            return False

    # ---- lifecycle ----
    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise PlatformError(self.platform_name, f"playwright 未安装: {e}")
            os.makedirs(self._user_data_dir, exist_ok=True)
            mobile = self._site == "h5"
            self._pw = await async_playwright().start()
            try:
                self._context = await self._pw.chromium.launch_persistent_context(
                    self._user_data_dir,
                    headless=self._headless,
                    slow_mo=self._slowmo,
                    user_agent=MOBILE_UA if mobile else DESKTOP_UA,
                    viewport={"width": 390, "height": 844} if mobile else {"width": 1280, "height": 900},
                    is_mobile=mobile,
                    has_touch=mobile,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
            except Exception as e:
                await self._safe_stop_pw()
                raise PlatformError(self.platform_name, f"启动浏览器失败(检查 Xvfb/DISPLAY 与 chromium 安装): {e}")
            self._context.set_default_timeout(self._timeout_ms)
            await self._context.add_init_script(_STEALTH_JS)
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
            self._started = True

    async def close(self) -> None:
        async with self._lock:
            try:
                if self._context is not None:
                    await self._context.close()
            except Exception:
                pass
            await self._safe_stop_pw()
            self._context = None
            self._page = None
            self._started = False

    async def _safe_stop_pw(self):
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None

    async def _ensure(self):
        if not self._started:
            await self.start()
        return self._page

    # ---- helpers ----
    async def _goto(self, url: str):
        page = await self._ensure()
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            raise PlatformError(self.platform_name, f"打开页面失败 {url}: {e}")
        await page.wait_for_timeout(1200)  # 给 JS 渲染留时间
        await self._check_blocked(page)
        return page

    async def _check_blocked(self, page):
        try:
            url = page.url or ""
            body = (await page.inner_text("body"))[:400] if await page.query_selector("body") else ""
        except Exception:
            url, body = page.url or "", ""
        hay = (url + " " + body).lower()
        for h in _BLOCK_HINTS:
            if h.lower() in hay:
                shot = await self._shot(page)
                raise PlatformError(
                    self.platform_name,
                    "触发淘宝安全验证/滑块，请打开 noVNC(:6080) 在浏览器里手动通过验证后重试。"
                    f" 当前页: {url}",
                )
        return False

    async def _shot(self, page) -> str:
        try:
            png = await page.screenshot(full_page=False)
            return "data:image/png;base64," + base64.b64encode(png).decode()
        except Exception:
            return ""

    async def _click_text(self, page, texts, timeout_ms: int = 6000) -> bool:
        """按可见文本点击第一个命中的按钮/链接。texts: 候选文案列表。"""
        for t in texts:
            loc = page.locator(f"text={t}").first
            try:
                await loc.wait_for(state="visible", timeout=timeout_ms)
                await loc.click()
                await page.wait_for_timeout(1000)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _item_id(url: str) -> str:
        m = _ID_RE.search(url or "")
        return m.group(1) if m else ""

    # ---- login ----
    async def check_login(self) -> LoginStatus:
        """基于登录 Cookie 判断(比抓 DOM 稳):淘宝登录后会写 unb / tracknick / lgc。"""
        await self._goto(MYTAOBAO_H5)
        try:
            cookies = await self._context.cookies()
        except Exception:
            cookies = []
        cmap = {c.get("name", ""): c.get("value", "") for c in cookies}
        logged_in = bool(cmap.get("unb") or cmap.get("tracknick") or cmap.get("_nk_"))
        nick = ""
        for k in ("tracknick", "lgc", "_nk_", "nick"):
            if cmap.get(k):
                from urllib.parse import unquote
                nick = unquote(cmap[k]).strip()[:40]
                break
        return LoginStatus(logged_in=logged_in, nick=nick, platform=self.platform_name)

    async def get_login_qrcode(self) -> LoginQrcode:
        page = await self._ensure()
        try:
            await page.goto(LOGIN_QR_URL, wait_until="domcontentloaded")
        except Exception as e:
            raise PlatformError(self.platform_name, f"打开登录页失败: {e}")
        await page.wait_for_timeout(2500)
        # TODO(selectors): 二维码容器随改版变化，尝试多个候选，失败则整页截图
        for sel in ("#J_QRCodeImg img", ".qrcode-img img", ".qrcode img", "canvas", '[class*="qrcode"] img'):
            el = await page.query_selector(sel)
            if el:
                try:
                    png = await el.screenshot()
                    return LoginQrcode(
                        qrcode_data_uri="data:image/png;base64," + base64.b64encode(png).decode(),
                        tip="用手机淘宝 App 扫码登录；登录后再调用其它工具。",
                        platform=self.platform_name,
                    )
                except Exception:
                    break
        full = await self._shot(page)
        return LoginQrcode(
            qrcode_data_uri=full,
            tip="未能精确定位二维码，已返回整页截图；请在图中找到二维码用手机淘宝扫码。若为滑块请用 noVNC 处理。",
            platform=self.platform_name,
        )

    # ---- browse ----
    async def search(self, keyword: str, limit: int) -> SearchResult:
        limit = min(self._max_results, max(1, limit))
        tmpl = SEARCH_H5 if self._site == "h5" else SEARCH_PC
        from urllib.parse import quote
        page = await self._goto(tmpl.format(q=quote(keyword)))
        # 通用抓取：扫描所有指向商品详情的链接，比 classname 稳。
        # TODO(selectors): 若结果为空，多半是未登录或触发风控，或 H5 结构调整。
        raw = await page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const anchors = Array.from(document.querySelectorAll('a[href*=\"item.htm\"], a[href*=\"/item/\"], a[href*=\"detail\"]'));
                for (const a of anchors) {
                    const href = a.href || '';
                    const m = href.match(/[?&]id=(\\d+)/);
                    const id = m ? m[1] : href;
                    if (!id || seen.has(id)) continue;
                    seen.add(id);
                    const box = a.closest('li,div,section') || a;
                    const text = (box.innerText || a.innerText || '').replace(/\\s+/g,' ').trim().slice(0,120);
                    out.push({id: (m?m[1]:''), href, text});
                    if (out.length >= 40) break;
                }
                return out;
            }"""
        )
        items = []
        for r in raw[:limit]:
            text = r.get("text", "")
            pm = _PRICE_RE.search(text)
            items.append(Item(
                item_id=r.get("id", ""),
                title=text,
                price=pm.group(1) if pm else "",
                url=r.get("href", ""),
            ))
        if not items:
            raise PlatformError(
                self.platform_name,
                "未解析到商品(可能未登录/触发风控/H5 改版)。可先 taobao_get_login_qrcode 登录，"
                "或用 noVNC 查看当前页；必要时校正 search 选择器。",
            )
        return SearchResult(keyword=keyword, count=len(items), items=items, platform=self.platform_name)

    async def get_item_detail(self, item: str) -> ItemDetail:
        url = item if item.startswith("http") else ITEM_H5.format(id=item)
        page = await self._goto(url)
        item_id = self._item_id(url)
        title = (await page.title() or "").strip()
        # TODO(selectors): 价格/销量/SKU 元素易变，尽力而为
        price = ""
        for sel in ('[class*="price"]', ".tb-rmb-num", '[class*="Price"]'):
            el = await page.query_selector(sel)
            if el:
                t = (await el.inner_text() or "").strip()
                pm = _PRICE_RE.search(t)
                if pm:
                    price = pm.group(1)
                    break
        skus = []
        for el in await page.query_selector_all('[class*="sku"] [class*="item"], .tb-sku .tb-metatit'):
            t = (await el.inner_text() or "").strip()
            if t and t not in skus:
                skus.append(t[:30])
            if len(skus) >= 30:
                break
        images = []
        for el in await page.query_selector_all('img[src*="alicdn"]'):
            src = await el.get_attribute("src")
            if src and src not in images:
                images.append(src)
            if len(images) >= 8:
                break
        return ItemDetail(
            item_id=item_id, title=title[:120], price=price,
            skus=skus, images=images, url=url,
        )

    # ---- cart / order ----
    async def add_to_cart(self, item: str, quantity: int, sku: str) -> ActionResult:
        quantity = min(self._max_qty, max(1, quantity))
        url = item if item.startswith("http") else ITEM_H5.format(id=item)
        page = await self._goto(url)
        if sku:
            # 尽力选择规格：按文本点击 sku 选项
            await self._click_text(page, [sku], timeout_ms=4000)
        clicked = await self._click_text(page, ["加入购物车", "加购物车", "加入购物袋"])
        if not clicked:
            shot = await self._shot(page)
            return ActionResult(
                ok=False,
                message="未找到“加入购物车”按钮(可能需要先选规格/登录，或按钮文案改版)。",
                url=page.url, screenshot_data_uri=shot, platform=self.platform_name,
            )
        await self._check_blocked(page)
        shot = await self._shot(page)
        return ActionResult(
            ok=True, message=f"已尝试加入购物车 x{quantity}" + (f"(规格:{sku})" if sku else ""),
            url=page.url, screenshot_data_uri=shot, platform=self.platform_name,
        )

    async def view_cart(self) -> CartView:
        page = await self._goto(CART_H5)
        lines = []
        raw = await page.evaluate(
            """() => {
                const out = [];
                const nodes = Array.from(document.querySelectorAll('li,div')).filter(n => {
                    const t = n.innerText || '';
                    return t.includes('¥') && t.length < 200 && n.querySelector('img');
                });
                for (const n of nodes.slice(0, 40)) {
                    out.push((n.innerText||'').replace(/\\s+/g,' ').trim().slice(0,160));
                }
                return out;
            }"""
        )
        seen = set()
        for t in raw:
            if t in seen:
                continue
            seen.add(t)
            pm = _PRICE_RE.search(t)
            lines.append(CartLine(title=t, price=pm.group(1) if pm else ""))
            if len(lines) >= 30:
                break
        return CartView(count=len(lines), lines=lines, url=page.url, platform=self.platform_name)

    async def create_order(self, item: Optional[str], quantity: int, sku: str) -> OrderDraft:
        """走到“确认订单/支付页”即停，绝不提交/付款。付款由人工经 noVNC 完成。"""
        quantity = min(self._max_qty, max(1, quantity))
        page = await self._ensure()
        if item:
            url = item if item.startswith("http") else ITEM_H5.format(id=item)
            await self._goto(url)
            if sku:
                await self._click_text(page, [sku], timeout_ms=4000)
            bought = await self._click_text(page, ["立即购买", "立即下单", "马上抢"])
            if not bought:
                shot = await self._shot(page)
                raise PlatformError(
                    self.platform_name,
                    "未找到“立即购买”按钮(可能需先选规格/登录)。已在返回错误，请用 noVNC 查看。",
                )
        else:
            # 从购物车结算
            await self._goto(CART_H5)
            await self._click_text(page, ["去结算", "结算", "去下单"])
        await page.wait_for_timeout(1500)
        await self._check_blocked(page)

        # 解析确认页金额：找“合计/实付款/¥”
        total = ""
        try:
            body = await page.inner_text("body")
        except Exception:
            body = ""
        m = re.search(r"(?:实付款|合计|应付|总计)[^\d]{0,6}(\d+(?:\.\d{1,2})?)", body)
        if not m:
            m = re.search(r"¥\s*(\d+(?:\.\d{1,2})?)", body)
        if m:
            total = m.group(1)

        shot = await self._shot(page)
        within = True
        stage = "confirm_order"
        if self._max_order_amount > 0 and total:
            try:
                if float(total) > self._max_order_amount:
                    within = False
                    stage = "blocked_amount_limit"
            except ValueError:
                pass
        next_action = (
            f"订单已停在确认页，未付款。请浏览器打开 http://<服务器IP>:6080/ (noVNC) "
            f"在 Chromium 里手动点击“提交订单”并在支付宝页确认付款。"
        )
        if not within:
            next_action = (
                f"金额 {total} 元超过上限 {self._max_order_amount} 元，已停止并未提交。"
                f"如确需购买，请调高 MCP_TAOBAO_MAX_ORDER_AMOUNT 或经 noVNC 人工核对后手动付款。"
            )
        return OrderDraft(
            stage=stage, total_amount=total, within_limit=within,
            url=page.url, screenshot_data_uri=shot,
            next_action=next_action, platform=self.platform_name,
        )
