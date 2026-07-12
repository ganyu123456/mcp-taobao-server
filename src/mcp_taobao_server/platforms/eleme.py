"""淘宝闪购/饿了么外卖 backend driven by a persistent Playwright browser context.

Grounded by a live spike:
- 登录 = 手机号 + 短信验证码 + 滑块(iframe ipassport.ele.me),登录后落到收货地址页。
  登录态是**会话级**,常驻进程内有效;进程重启需重登。→ 服务器保持单一常驻进程。
- 收货地址页:``h5.ele.me/minisite/pages-poi/address/index``,列出常用地址。
- 选地址后进入 msite 首页(附近门店)。

⚠️ 登录/地址流程已实测;搜店/店内菜单/加购/下单的选择器是**最佳猜测**(标 ``TODO(selectors)``),
   需在真实页面校正(明天实机)。所有方法找不到元素时返回结构化结果/错误,不崩溃。
   服务器无屏时用 Xvfb 虚拟有头 + noVNC:登录的短信/滑块、以及最终付款,都由人经 noVNC 完成。
"""

import asyncio
import base64
import os
import re
from typing import Optional

from .base import (
    BasePlatform, PlatformError,
    LoginStatus, Address, AddressList, Shop, SearchResult,
    MenuItem, ShopMenu, ActionResult, CartLine, CartView, OrderDraft,
)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
)

# --- URLs (TODO(selectors): 饿了么改版在此集中调整) ---
MSITE = "https://h5.ele.me/msite/"
ADDRESS_URL = "https://h5.ele.me/minisite/pages-poi/address/index"
SEARCH_URL = "https://h5.ele.me/restapi/shopping/v3/search?keyword={q}"   # 备用；默认走页面搜索
SEARCH_PAGE = "https://h5.ele.me/search/?keyword={q}"

_BLOCK_HINTS = ("滑块", "拖动", "验证码", "安全验证", "punish", "//sec.", "captcha")
_STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_PRICE_RE = re.compile(r"(\d+(?:\.\d{1,2})?)")


class ElemePlatform(BasePlatform):
    def __init__(
        self,
        user_data_dir: str,
        headless: bool = False,
        timeout: int = 30,
        lat: float = 32.06,
        lng: float = 118.80,
        max_results: int = 10,
        max_order_amount: float = 100.0,
        slowmo: int = 0,
    ):
        self._user_data_dir = os.path.abspath(os.path.expanduser(user_data_dir))
        self._headless = headless
        self._timeout_ms = max(5, timeout) * 1000
        self._lat = lat
        self._lng = lng
        self._max_results = max(1, max_results)
        self._max_order_amount = max(0.0, max_order_amount)
        self._slowmo = max(0, slowmo)

        self._pw = None
        self._context = None
        self._page = None
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def platform_name(self) -> str:
        return "eleme"

    def is_available(self) -> bool:
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
            self._pw = await async_playwright().start()
            try:
                self._context = await self._pw.chromium.launch_persistent_context(
                    self._user_data_dir,
                    headless=self._headless,
                    slow_mo=self._slowmo,
                    user_agent=MOBILE_UA,
                    viewport={"width": 390, "height": 844},
                    is_mobile=True,
                    has_touch=True,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    geolocation={"latitude": self._lat, "longitude": self._lng},
                    permissions=["geolocation"],
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
    async def _goto(self, url: str, wait: int = 4000):
        page = await self._ensure()
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            raise PlatformError(self.platform_name, f"打开页面失败 {url}: {e}")
        await page.wait_for_timeout(wait)
        return page

    async def _shot(self, page) -> str:
        try:
            png = await page.screenshot(full_page=False)
            return "data:image/png;base64," + base64.b64encode(png).decode()
        except Exception:
            return ""

    def _need_login(self, page) -> bool:
        return "/login" in (page.url or "")

    async def _require_login(self, page):
        if self._need_login(page):
            raise PlatformError(
                self.platform_name,
                "未登录饿了么。请先调 shangou_open_login,并在 noVNC(:6080) 里用手机号+短信验证码+滑块完成登录。",
            )

    async def _click_text(self, page, texts, timeout_ms: int = 5000) -> bool:
        for t in texts:
            loc = page.locator(f"text={t}").first
            try:
                await loc.wait_for(state="visible", timeout=timeout_ms)
                await loc.click()
                await page.wait_for_timeout(1200)
                return True
            except Exception:
                continue
        return False

    # ---- login ----
    async def check_login(self) -> LoginStatus:
        page = await self._goto(MSITE, wait=5000)
        logged_in = not self._need_login(page)
        return LoginStatus(logged_in=logged_in, platform=self.platform_name)

    async def open_login(self) -> ActionResult:
        page = await self._goto(MSITE, wait=5000)
        if not self._need_login(page):
            return ActionResult(ok=True, message="已登录饿了么,无需重复登录。",
                                url=page.url, platform=self.platform_name)
        shot = await self._shot(page)
        return ActionResult(
            ok=False,
            message=("已打开饿了么登录页。请在 noVNC(http://<服务器IP>:6080/vnc.html) 的浏览器窗口里: "
                     "输入手机号 → 获取并填写短信验证码 → 拖动滑块 → 点『同意协议并登录』。"
                     "登录态在本进程存活期间有效。"),
            url=page.url, screenshot_data_uri=shot, platform=self.platform_name,
        )

    # ---- address ----
    async def list_addresses(self) -> AddressList:
        page = await self._goto(ADDRESS_URL, wait=4000)
        await self._require_login(page)
        # 每个地址行含 11 位手机号;按行提取
        raw = await page.evaluate(r"""() => {
            const out=[]; const seen=new Set();
            for(const e of Array.from(document.querySelectorAll('div,li,section'))){
                const t=(e.innerText||'').replace(/\s+/g,' ').trim();
                if(t && t.length<120 && /1[3-9]\d{9}/.test(t) && (t.includes('编辑')||t.includes('删除'))){
                    if(seen.has(t)) continue; seen.add(t); out.push(t);
                }
            }
            return out.slice(0,20);
        }""")
        addrs = []
        for i, t in enumerate(raw):
            pm = _PHONE_RE.search(t)
            contact = t[pm.start():] if pm else ""
            label = t[:pm.start()].replace("编辑", "").replace("删除", "").replace("常用", "").strip() if pm else t
            addrs.append(Address(index=i, label=label[:40], detail="", contact=contact[:40],
                                 current="当前" in t))
        return AddressList(count=len(addrs), addresses=addrs, url=page.url, platform=self.platform_name)

    async def set_address(self, keyword: str, index: int) -> ActionResult:
        page = await self._goto(ADDRESS_URL, wait=4000)
        await self._require_login(page)
        clicked = False
        if keyword:
            clicked = await self._click_text(page, [keyword], timeout_ms=4000)
        if not clicked:
            # 按序号点第 index 个含手机号的地址行
            try:
                rows = page.locator("div,li").filter(has_text=_PHONE_RE.pattern)
                await rows.nth(max(0, index)).click(timeout=4000)
                clicked = True
            except Exception:
                clicked = False
        await page.wait_for_timeout(5000)
        shot = await self._shot(page)
        if not clicked:
            return ActionResult(ok=False, message="未能选中地址(关键词不匹配或页面改版),请用 noVNC 手动选一次。",
                                url=page.url, screenshot_data_uri=shot, platform=self.platform_name)
        return ActionResult(ok=True, message="已选择收货地址,进入附近门店。",
                            url=page.url, screenshot_data_uri=shot, platform=self.platform_name)

    # ---- browse ----
    async def search(self, keyword: str, limit: int) -> SearchResult:
        limit = min(self._max_results, max(1, limit))
        from urllib.parse import quote
        page = await self._goto(SEARCH_PAGE.format(q=quote(keyword)), wait=5000)
        await self._require_login(page)
        # TODO(selectors): 饿了么搜索结果结构未实机校正,尽力抓店铺卡片
        raw = await page.evaluate(r"""() => {
            const out=[]; const seen=new Set();
            const cards = Array.from(document.querySelectorAll('a[href*="shop"], [class*="shop"], [class*="restaurant"], [class*="Shop"]'));
            for(const c of cards){
                const t=(c.innerText||'').replace(/\s+/g,' ').trim();
                if(!t || t.length<2 || t.length>120 || seen.has(t)) continue;
                seen.add(t);
                const a=c.matches('a')?c:c.querySelector('a');
                const href=a?a.href:'';
                const m=href.match(/(?:shopId=|\/shop\/)(\w+)/);
                out.push({name:t.slice(0,50), href, id:m?m[1]:''});
                if(out.length>=30) break;
            }
            return out;
        }""")
        shops = []
        for r in raw[:limit]:
            shops.append(Shop(shop_id=r.get("id", ""), name=r.get("name", ""), url=r.get("href", "")))
        if not shops:
            raise PlatformError(
                self.platform_name,
                "未解析到店铺(未选地址/未登录/风控/搜索页结构变化)。可先 shangou_set_address 设地址,"
                "或用 noVNC 查看当前页;必要时校正 search 选择器。",
            )
        return SearchResult(keyword=keyword, count=len(shops), shops=shops,
                            url=page.url, platform=self.platform_name)

    async def shop_menu(self, shop: str, limit: int) -> ShopMenu:
        limit = min(self._max_results, max(1, limit))
        url = shop if shop.startswith("http") else f"https://h5.ele.me/shop/?id={shop}"
        page = await self._goto(url, wait=5000)
        await self._require_login(page)
        shop_name = (await page.title() or "").strip()[:40]
        # TODO(selectors): 店内菜单结构未实机校正
        raw = await page.evaluate(r"""() => {
            const out=[];
            const nodes = Array.from(document.querySelectorAll('[class*="food"], [class*="menu"], [class*="dish"], li')).filter(n=>{
                const t=n.innerText||''; return t.includes('¥') && t.length<160;
            });
            for(const n of nodes.slice(0,40)){
                out.push((n.innerText||'').replace(/\s+/g,' ').trim().slice(0,120));
            }
            return out;
        }""")
        items, seen = [], set()
        for t in raw:
            if t in seen:
                continue
            seen.add(t)
            pm = _PRICE_RE.search(t)
            items.append(MenuItem(name=t, price=pm.group(1) if pm else ""))
            if len(items) >= limit:
                break
        return ShopMenu(shop_id=shop, shop_name=shop_name, count=len(items), items=items,
                        url=page.url, platform=self.platform_name)

    # ---- cart / order ----
    async def add_to_cart(self, shop: str, item: str, quantity: int) -> ActionResult:
        quantity = max(1, quantity)
        url = shop if shop.startswith("http") else f"https://h5.ele.me/shop/?id={shop}"
        page = await self._goto(url, wait=5000)
        await self._require_login(page)
        # 先滚动定位菜品,再点其加号(TODO(selectors))
        if item:
            try:
                await page.get_by_text(item, exact=False).first.scroll_into_view_if_needed(timeout=4000)
            except Exception:
                pass
        added = 0
        for _ in range(quantity):
            ok = await self._click_text(page, ["加入购物车", "＋", "+", "选规格"], timeout_ms=3000)
            if not ok:
                break
            added += 1
        shot = await self._shot(page)
        if added == 0:
            return ActionResult(ok=False, message="未找到菜品加号(需先定位菜品/选规格,或选择器需校正)。",
                                url=page.url, screenshot_data_uri=shot, platform=self.platform_name)
        return ActionResult(ok=True, message=f"已尝试加入购物车 x{added}",
                            url=page.url, screenshot_data_uri=shot, platform=self.platform_name)

    async def view_cart(self, shop: str) -> CartView:
        url = shop if shop.startswith("http") else f"https://h5.ele.me/shop/?id={shop}"
        page = await self._goto(url, wait=4000)
        await self._require_login(page)
        raw = await page.evaluate(r"""() => {
            const out=[];
            for(const n of Array.from(document.querySelectorAll('[class*="cart"] *, [class*="Cart"] *'))){
                const t=(n.innerText||'').replace(/\s+/g,' ').trim();
                if(t && t.includes('¥') && t.length<80) out.push(t);
            }
            return Array.from(new Set(out)).slice(0,20);
        }""")
        lines = []
        for t in raw:
            pm = _PRICE_RE.search(t)
            lines.append(CartLine(name=t[:60], price=pm.group(1) if pm else ""))
        return CartView(count=len(lines), lines=lines, url=page.url, platform=self.platform_name)

    async def create_order(self, shop: str) -> OrderDraft:
        """点『去结算』到确认订单/支付页即停,不提交、不付款。"""
        url = shop if shop.startswith("http") else f"https://h5.ele.me/shop/?id={shop}"
        page = await self._goto(url, wait=4000)
        await self._require_login(page)
        ok = await self._click_text(page, ["去结算", "去下单", "结算"])
        if not ok:
            shot = await self._shot(page)
            raise PlatformError(self.platform_name,
                                "未找到『去结算』(购物车可能为空或选择器需校正)。请用 noVNC 查看。")
        await page.wait_for_timeout(4000)
        try:
            body = await page.inner_text("body")
        except Exception:
            body = ""
        m = re.search(r"(?:实付款|合计|待支付|应付|总计)[^\d]{0,6}(\d+(?:\.\d{1,2})?)", body)
        if not m:
            m = re.search(r"¥\s*(\d+(?:\.\d{1,2})?)", body)
        total = m.group(1) if m else ""
        within = True
        stage = "confirm_order"
        if self._max_order_amount > 0 and total:
            try:
                if float(total) > self._max_order_amount:
                    within, stage = False, "blocked_amount_limit"
            except ValueError:
                pass
        shot = await self._shot(page)
        next_action = ("订单已停在确认/支付页,未付款。请打开 http://<服务器IP>:6080/vnc.html (noVNC) "
                       "在浏览器里手动确认并完成支付。")
        if not within:
            next_action = (f"金额 {total} 元超过上限 {self._max_order_amount} 元,已停止未提交。"
                           f"如确需下单,调高 MCP_TAOBAO_MAX_ORDER_AMOUNT 或经 noVNC 人工核对后手动付款。")
        return OrderDraft(stage=stage, total_amount=total, within_limit=within,
                          url=page.url, screenshot_data_uri=shot,
                          next_action=next_action, platform=self.platform_name)
