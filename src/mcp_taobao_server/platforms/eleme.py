"""淘宝闪购/饿了么外卖 backend driven by a persistent Playwright browser context.

Calibrated against the live H5 flow (2026-07-13):
- 登录 = 手机号 + 短信验证码 + 滑块(iframe ipassport.ele.me),登录后落到收货地址页。
  登录态是**会话级**,常驻进程内有效;进程重启需重登。→ 服务器保持单一常驻进程。
- 收货地址页:``h5.ele.me/minisite/pages-poi/address/index``,列出常用地址。
- 搜索页:``h5.ele.me/search/?keyword=X`` 302→ ``h5.ele.me/minisearch/result?...``,
  是 **tiga/Rax 影子DOM**(无 <a>,靠 Playwright 文本/tiga 定位穿透 shadow DOM);
  一屏若干商超卡片,"左滑进店"只是提示,进店靠**点击店铺卡片头部**。
- 进店后是 **淘宝闪购 ``h5.ele.me/newretail/p/ushop/?store_id=...``(普通 HTML)**,
  加购按钮 ``aria-label="加购, 按钮"``;店内搜索入口 ``aria-label="搜索店内商品"`` →
  ``h5.ele.me/newretail/p/ushopsearch/``。
- 购物车金额读底栏 ``aria-label="购物车总计金额X元..."``;未达起送时为 ``差¥X起送``。
- 结算 ``去结算`` → ``h5.ele.me/newretail/tr/buy/`` 确认订单页,读『合计』金额后**即停,不提交、不付款**。

服务器无屏时用 Xvfb 虚拟有头 + noVNC:登录的短信/滑块、以及最终付款,都由人经 noVNC 完成。
"""

import asyncio
import base64
import os
import re
from urllib.parse import quote

from .base import (
    BasePlatform, PlatformError,
    LoginStatus, Address, AddressList, Shop, SearchResult,
    MenuItem, ShopMenu, ActionResult, CartLine, CartView, OrderDraft,
)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
)

MSITE = "https://h5.ele.me/msite/"
ADDRESS_URL = "https://h5.ele.me/minisite/pages-poi/address/index"
SEARCH_PAGE = "https://h5.ele.me/search/?keyword={q}"   # 302 → minisearch/result

# URL 片段(判定当前处于哪个页面)
_URL_MINISEARCH = "minisearch/result"
_URL_USHOP = "newretail/p/ushop"
_URL_USHOPSEARCH = "newretail/p/ushopsearch"
_URL_BUY = "newretail/tr/buy"

_BLOCK_HINTS = ("滑块", "拖动", "验证码", "安全验证", "punish", "//sec.", "captcha")
_STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_STORE_ID_RE = re.compile(r"store_id=(\d+)")
_CART_TOTAL_RE = re.compile(r"购物车总计金额([\d.]+)元")
_QISONG_GAP_RE = re.compile(r"差¥?([\d.]+)起送")


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

        # 会话内浏览状态
        self._search_keyword = ""
        self._shop_index = -1          # 当前已进入的店铺序号(对应 search 返回的 shop_id)
        self._store_id = ""
        self._last_cart_total = ""     # 点『去结算』前抓到的购物车总计,作为金额兜底

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
            loc = page.get_by_text(t, exact=False).first
            try:
                await loc.wait_for(state="visible", timeout=timeout_ms)
                await loc.click()
                await page.wait_for_timeout(1200)
                return True
            except Exception:
                continue
        return False

    async def _cart_status(self, page):
        """读底栏购物车状态:返回 (total:str, gap:str, can_checkout:bool)。"""
        total, gap = "", ""
        try:
            labels = await page.eval_on_selector_all(
                "[aria-label]", "els => els.map(e => e.getAttribute('aria-label'))"
            )
        except Exception:
            labels = []
        for lb in labels or []:
            if not lb:
                continue
            if not total:
                m = _CART_TOTAL_RE.search(lb)
                if m:
                    total = m.group(1)
            if not gap:
                g = _QISONG_GAP_RE.search(lb)
                if g:
                    gap = g.group(1)
        can = False
        try:
            can = (await page.get_by_text("去结算", exact=False).count()) > 0
        except Exception:
            can = False
        return total, gap, can

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
        """选定收货地址。地址页是 tiga 影子DOM,选择需点行体(避开右侧编辑/删除),用坐标 tap。"""
        page = await self._goto(ADDRESS_URL, wait=4500)
        await self._require_login(page)
        leafs = await self._pierce_texts(page)
        # 地址行:x∈[35,75] 且同一行右侧有『编辑』的文本,即一条保存地址
        edit_ys = [y for (y, x, s) in leafs if s.strip() == "编辑"]
        rows = []
        for (y, x, s) in leafs:
            if 35 <= x <= 75 and len(s) >= 4 and any(abs(y - ey) <= 16 for ey in edit_ys):
                rows.append((y, s))
        rows.sort(key=lambda z: z[0])

        target_y, target_name = None, ""
        if keyword:
            for (y, s) in rows:
                if keyword in s:
                    target_y, target_name = y, s
                    break
            if target_y is None:   # 关键词也匹配行内其它叶子(如门牌)
                for (y, x, s) in sorted(leafs, key=lambda z: z[0]):
                    if keyword in s and 35 <= x <= 300:
                        target_y, target_name = y, s
                        break
        if target_y is None and rows:
            i = min(max(0, index), len(rows) - 1)
            target_y, target_name = rows[i]

        if target_y is None:
            shot = await self._shot(page)
            return ActionResult(ok=False, message="未找到可选地址(页面为空/未登录/结构变化),请用 noVNC 手动选一次。",
                                url=page.url, screenshot_data_uri=shot, platform=self.platform_name)

        # 点该行左中部(x=150 远离右侧 编辑/删除 x>320)选定;通常跳回门店首页
        await page.touchscreen.tap(150, float(target_y))
        for _ in range(12):
            await page.wait_for_timeout(600)
            if "address" not in (page.url or ""):
                break
        await page.wait_for_timeout(2500)
        selected = "address" not in (page.url or "")
        shot = await self._shot(page)
        if not selected:
            return ActionResult(ok=False,
                                message=f"已尝试选择『{target_name}』但页面未跳转,可能需人工在 noVNC 里点一次。",
                                url=page.url, screenshot_data_uri=shot, platform=self.platform_name)
        return ActionResult(ok=True, message=f"已选择收货地址『{target_name}』,进入附近门店。",
                            url=page.url, screenshot_data_uri=shot, platform=self.platform_name)

    # ---- browse ----
    async def search(self, keyword: str, limit: int) -> SearchResult:
        """搜索附近商超(minisearch/result,tiga 影子DOM)。shop_id = 结果序号,供后续进店。"""
        limit = min(self._max_results, max(1, limit))
        page = await self._goto(SEARCH_PAGE.format(q=quote(keyword)), wait=6000)
        await self._require_login(page)
        self._search_keyword = keyword
        self._shop_index = -1
        self._store_id = ""

        anchors = await self._shop_anchors(page)   # 每店一个『起送』锚(tiga-text)
        leafs = await self._pierce_texts(page)     # 全量穿透 shadow DOM 的叶子文本

        _STOP = ("综合排序", "销量优先", "速度优先", "筛选", "清空", "查看", "距离优先",
                 "人均价低到高", "商家好评优先", "起送低到高", "左滑进店", "蜂鸟准时达",
                 "切换", "地址", "新客免配送费")
        shops = []
        for i, (ay, _abox) in enumerate(anchors[:limit]):
            band_lo, band_hi = ay - 110, ay + 45
            band = sorted((t for t in leafs if band_lo <= t[0] <= band_hi),
                          key=lambda z: (z[0], z[1]))
            joined = " ".join(s for (_ty, _tx, s) in band)
            # 店名:在『配送品牌行』(蜂鸟准时达/商家自配送)上取最左侧的非数字文本
            name = ""
            row_y = None
            for (ty, _tx, s) in band:
                if "准时达" in s or "自配送" in s:
                    row_y = ty
                    break
            if row_y is not None:
                cands = sorted(
                    (tx, s) for (ty, tx, s) in band
                    if abs(ty - row_y) <= 12 and 55 <= tx <= 250
                    and len(s) >= 2 and not re.fullmatch(r"[\d.]+", s)
                    and "准时达" not in s and "自配送" not in s and s not in _STOP
                )
                if cands:
                    name = cands[0][1]
            shops.append(Shop(
                shop_id=str(i),
                name=name,
                rating=self._first(r"([\d.]+)\s*分(?!钟)", joined),
                eta=self._first(r"(\d+)\s*分钟", joined),
                distance=self._first(r"([\d.]+)\s*km", joined),
                url="",
            ))

        if not shops:
            raise PlatformError(
                self.platform_name,
                "未解析到店铺(未选地址/未登录/风控/搜索页结构变化)。可先 shangou_set_address 设地址,"
                "或用 noVNC 查看当前页。",
            )
        return SearchResult(keyword=keyword, count=len(shops), shops=shops,
                            url=page.url, platform=self.platform_name)

    async def _shop_anchors(self, page):
        """返回搜索结果页每个店铺卡片的『起送』锚点:[(y_center, box)],按 y 排序。"""
        out = []
        loc = page.locator("tiga-text")
        try:
            n = await loc.count()
        except Exception:
            return out
        for i in range(min(n, 250)):
            el = loc.nth(i)
            try:
                s = (await el.inner_text()).strip()
            except Exception:
                continue
            if "起送" not in s:
                continue
            try:
                b = await el.bounding_box()
            except Exception:
                b = None
            if b:
                out.append((b["y"] + b["height"] / 2, b))
        out.sort(key=lambda z: z[0])
        return out

    async def _pierce_texts(self, page):
        """递归穿透所有 shadow root,采集叶子文本 (y_center, x, text)。

        minisearch 结果页是 tiga 影子DOM,``evaluate`` 默认看不到内部;这里手动
        递归 ``el.shadowRoot`` 把店名/评分/距离等全量捞出,再按卡片坐标归组。
        """
        try:
            raw = await page.evaluate(r"""() => {
                const out=[];
                function visit(node){
                    let kids; try { kids = node.querySelectorAll('*'); } catch(e){ return; }
                    for(const el of kids){
                        if(el.shadowRoot) visit(el.shadowRoot);
                        if(el.childElementCount===0){
                            const t=(el.textContent||'').replace(/\s+/g,' ').trim();
                            if(t && t.length<=30){
                                const r=el.getBoundingClientRect();
                                if(r.width>0 && r.height>0) out.push([r.y+r.height/2, r.x, t]);
                            }
                        }
                    }
                }
                visit(document);
                return out.slice(0,600);
            }""")
        except Exception:
            raw = []
        return [(float(y), float(x), s) for (y, x, s) in raw]

    @staticmethod
    def _first(pattern, text, default=""):
        m = re.search(pattern, text)
        return m.group(1) if m else default

    def _buy_amount(self, url: str) -> str:
        """从结算页 URL 参数取实付金额:realPayPrice(分) 优先,回退 t_pri(元)。"""
        mp = re.search(r"realPayPrice(?:%22%3A|\"?:)(\d+)", url or "")
        if mp:
            return f"{int(mp.group(1)) / 100:.2f}"
        return self._first(r"[?&]t_pri=([\d.]+)", url or "")

    async def _enter_shop(self, index: int):
        """进入 search 结果里第 index 家店:必要时回搜索页,点击店铺卡片进入 ushop。"""
        page = await self._ensure()
        if self._shop_index == index and _URL_USHOP in (page.url or ""):
            return page
        # 确保在搜索结果页
        if _URL_MINISEARCH not in (page.url or ""):
            if not self._search_keyword:
                raise PlatformError(self.platform_name, "尚未搜索。请先调 shangou_search。")
            page = await self._goto(SEARCH_PAGE.format(q=quote(self._search_keyword)), wait=6000)
            await self._require_login(page)
        anchors = await self._shop_anchors(page)
        if index < 0 or index >= len(anchors):
            raise PlatformError(self.platform_name,
                                f"店铺序号 {index} 越界(本次搜索共 {len(anchors)} 家)。")
        # 滚动到该卡片,点其头部(『起送』行上方约 32px = 店名/logo 区域)进入
        anchor_loc = page.locator("tiga-text").filter(has_text="起送")
        try:
            await anchor_loc.nth(index).scroll_into_view_if_needed(timeout=4000)
            await page.wait_for_timeout(800)
        except Exception:
            pass
        anchors = await self._shop_anchors(page)
        if index >= len(anchors):
            raise PlatformError(self.platform_name, "进店失败:卡片定位丢失,请重试或用 noVNC 查看。")
        yc, _b = anchors[index]
        tap_y = max(70.0, yc - 32)
        await page.touchscreen.tap(150, tap_y)
        # 等待进入 ushop
        for _ in range(20):
            await page.wait_for_timeout(600)
            if _URL_USHOP in (page.url or ""):
                break
        if _URL_USHOP not in (page.url or ""):
            raise PlatformError(self.platform_name,
                                "点击店铺卡片未能进店(页面改版或风控)。请用 noVNC 手动进店后重试。")
        await page.wait_for_timeout(3000)
        self._shop_index = index
        m = _STORE_ID_RE.search(page.url or "")
        self._store_id = m.group(1) if m else ""
        return page

    def _resolve_index(self, shop: str) -> int:
        s = (shop or "").strip()
        if s == "" and self._shop_index >= 0:
            return self._shop_index
        if s.isdigit():
            return int(s)
        if self._shop_index >= 0:
            return self._shop_index
        raise PlatformError(self.platform_name,
                            "shop 需为 shangou_search 返回的 shop_id(序号)。请先搜索并传入序号。")

    async def shop_menu(self, shop: str, limit: int) -> ShopMenu:
        limit = min(self._max_results, max(1, limit))
        index = self._resolve_index(shop)
        page = await self._enter_shop(index)
        shop_name = (await page.title() or "").strip()[:40]
        rows = await page.evaluate(r"""(LIMIT) => {
            const out=[];
            const btns = Array.from(document.querySelectorAll('[aria-label]'))
                .filter(e => (e.getAttribute('aria-label')||'').indexOf('加购') >= 0);
            for(const b of btns){
                const rb=b.getBoundingClientRect(); const yc=rb.y+rb.height/2;
                let name='', price='';
                for(const el of document.querySelectorAll('div,span')){
                    const r=el.getBoundingClientRect();
                    if(Math.abs((r.y+r.height/2)-yc)>60) continue;
                    if(r.x>330 || r.width===0) continue;
                    if(el.childElementCount!==0) continue;
                    const t=(el.textContent||'').replace(/\s+/g,' ').trim();
                    if(!t || t.length>=40 || /^¥/.test(t)) continue;
                    if(t.length>name.length) name=t;
                }
                for(const el of document.querySelectorAll('[aria-label]')){
                    const l=el.getAttribute('aria-label')||''; if(l.indexOf('价格信息')<0) continue;
                    const r=el.getBoundingClientRect(); if(Math.abs((r.y+r.height/2)-yc)>60) continue;
                    const m=l.match(/([\d.]+)/); if(m){ price=m[1]; break; }
                }
                if(name) out.push({name, price});
                if(out.length>=LIMIT) break;
            }
            return out;
        }""", limit)
        items, seen = [], set()
        for r in rows:
            nm = r.get("name", "")
            if not nm or nm in seen:
                continue
            seen.add(nm)
            items.append(MenuItem(name=nm, price=r.get("price", "")))
        return ShopMenu(shop_id=str(index), shop_name=shop_name, count=len(items), items=items,
                        url=page.url, platform=self.platform_name)

    # ---- cart / order ----
    async def _in_shop_search(self, page, item: str):
        """在店内搜索指定商品(ushop → ushopsearch)。"""
        inp = page.locator("input")
        if await inp.count() == 0:
            entry = page.locator('[aria-label="搜索店内商品"]').first
            try:
                await entry.click(timeout=4000)
                await page.wait_for_timeout(2500)
            except Exception:
                raise PlatformError(self.platform_name, "未找到店内搜索入口(页面改版)。")
            inp = page.locator("input")
        if await inp.count() == 0:
            raise PlatformError(self.platform_name, "未找到店内搜索输入框。")
        await inp.first.click(timeout=3000)
        await inp.first.fill(item)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(4000)

    async def add_to_cart(self, shop: str, item: str, quantity: int) -> ActionResult:
        quantity = max(1, quantity)
        index = self._resolve_index(shop)
        page = await self._enter_shop(index)
        if item:
            await self._in_shop_search(page, item)
        btns = page.locator('[aria-label*="加购"]')
        if await btns.count() == 0:
            shot = await self._shot(page)
            return ActionResult(ok=False, message=f"未找到『{item}』的加购按钮(可能无货或页面改版)。",
                                url=page.url, screenshot_data_uri=shot, platform=self.platform_name)
        # 首个结果 = 目标商品;记录其行 y,便于加数量时点同一行的 +
        first = btns.first
        try:
            fb = await first.bounding_box()
        except Exception:
            fb = None
        yc = (fb["y"] + fb["height"] / 2) if fb else None
        added = 0
        for _ in range(quantity):
            btns = page.locator('[aria-label*="加购"], [aria-label*="增加"], [aria-label*="添加"]')
            n = await btns.count()
            if n == 0:
                break
            target = 0
            if yc is not None:
                best, bestdy = 0, 1e9
                for i in range(n):
                    try:
                        b = await btns.nth(i).bounding_box()
                    except Exception:
                        b = None
                    if not b:
                        continue
                    dy = abs((b["y"] + b["height"] / 2) - yc)
                    if dy < bestdy:
                        bestdy, best = dy, i
                if bestdy <= 60:
                    target = best
            try:
                await btns.nth(target).click(timeout=3000)
                added += 1
                await page.wait_for_timeout(1000)
            except Exception:
                break
        total, gap, can = await self._cart_status(page)
        shot = await self._shot(page)
        if added == 0:
            return ActionResult(ok=False, message=f"『{item}』加购失败(未能点击加购按钮)。",
                                url=page.url, screenshot_data_uri=shot, platform=self.platform_name)
        msg = f"已加购『{item}』x{added}。购物车总计 ¥{total or '?'}。"
        if not can and gap:
            msg += f"未达起送,还差 ¥{gap},请继续加购。"
        elif can:
            msg += "已达起送,可 shangou_create_order 生成订单。"
        return ActionResult(ok=True, message=msg, url=page.url,
                            screenshot_data_uri=shot, platform=self.platform_name)

    async def view_cart(self, shop: str) -> CartView:
        index = self._resolve_index(shop)
        page = await self._ensure()
        if not (self._shop_index == index and _URL_USHOP in (page.url or "")):
            page = await self._enter_shop(index)
        total, gap, can = await self._cart_status(page)
        # 尝试展开购物车面板读取明细
        lines = []
        try:
            panel = page.locator('[aria-label*="购物车总计金额"]').first
            if await panel.count() > 0:
                await panel.click(timeout=3000)
                await page.wait_for_timeout(1500)
                raw = await page.evaluate(r"""() => {
                    const out=[];
                    for(const el of document.querySelectorAll('[aria-label]')){
                        const l=el.getAttribute('aria-label')||'';
                        if(l.indexOf('价格信息')>=0){
                            const r=el.getBoundingClientRect();
                            out.push({price:(l.match(/([\d.]+)/)||[,''])[1], y:Math.round(r.y)});
                        }
                    }
                    return out.slice(0,30);
                }""")
                for r in raw:
                    lines.append(CartLine(name="", price=r.get("price", "")))
        except Exception:
            pass
        return CartView(count=len(lines), lines=lines, total=total or "",
                        url=page.url, platform=self.platform_name)

    async def create_order(self, shop: str) -> OrderDraft:
        """点『去结算』到确认订单页即停,不提交、不付款。"""
        index = self._resolve_index(shop)
        page = await self._ensure()
        if not (self._shop_index == index and (_URL_USHOP in (page.url or "") or _URL_USHOPSEARCH in (page.url or ""))):
            page = await self._enter_shop(index)

        total, gap, can = await self._cart_status(page)
        self._last_cart_total = total or self._last_cart_total
        if not can:
            hint = f"购物车未达起送,还差 ¥{gap}。" if gap else "购物车为空或未达起送。"
            return OrderDraft(stage="below_min_order", total_amount="", within_limit=True,
                              shop=str(index), url=page.url,
                              next_action=hint + "请继续 shangou_add_to_cart 加购后再下单。",
                              platform=self.platform_name)

        if not await self._click_text(page, ["去结算"]):
            shot = await self._shot(page)
            raise PlatformError(self.platform_name, "未能点击『去结算』。请用 noVNC 查看购物车。")
        for _ in range(20):
            await page.wait_for_timeout(600)
            if _URL_BUY in (page.url or ""):
                break
        await page.wait_for_timeout(3000)

        # 金额:优先从结算页 URL 参数取(最可靠),回退页面正则/购物车总计。
        buy_url = page.url or ""
        amount = self._buy_amount(buy_url)
        if not amount:
            try:
                body = await page.inner_text("body")
            except Exception:
                body = ""
            packed = re.sub(r"\s+", "", body)
            amount = self._first(r"合计(?:已优惠¥[\d.]+)?¥([\d.]+)", packed) \
                or self._first(r"(?:实付款|实付|需支付|应付|待支付)¥?([\d.]+)", packed) \
                or self._last_cart_total
        try:
            addr = self._first(r"(?:送货到家|到店自取)(.+?)(?:根据当前|立即送出|约\d)",
                               re.sub(r"\s+", "", await page.inner_text("body")))
        except Exception:
            addr = ""

        within, stage = True, "confirm_order"
        if self._max_order_amount > 0 and amount:
            try:
                if float(amount) > self._max_order_amount:
                    within, stage = False, "blocked_amount_limit"
            except ValueError:
                pass
        shot = await self._shot(page)
        if within:
            next_action = ("订单已停在确认页,未提交、未付款。核对金额无误后:调 shangou_submit_order 提交并拿到"
                           "支付宝收银台链接;或在 noVNC(:6080)手动点『提交订单』付款。")
        else:
            next_action = (f"金额 ¥{amount} 超过上限 ¥{self._max_order_amount},已停止未提交。"
                           f"如确需下单,调高 MCP_TAOBAO_MAX_ORDER_AMOUNT 或经 noVNC 人工核对后手动付款。")
        return OrderDraft(stage=stage, total_amount=amount, within_limit=within,
                          address=addr[:60], shop=str(index),
                          url=page.url, screenshot_data_uri=shot,
                          next_action=next_action, platform=self.platform_name)

    async def submit_order(self, shop: str) -> OrderDraft:
        """在确认订单页点『提交订单』,进入支付宝收银台即停(不输密码、不代付)。

        ⚠️ 会**真实创建订单**。仅在用户明确同意下单时调用。收银台链接与登录态/时效绑定,
        请在【已登录的浏览器】(服务器为 noVNC:6080)打开完成支付。
        """
        index = self._resolve_index(shop)
        page = await self._ensure()
        # 不在确认页则先走到确认页(会校验起送/金额上限)
        if _URL_BUY not in (page.url or ""):
            draft = await self.create_order(shop)
            if draft.stage != "confirm_order":
                return draft
            page = self._page

        amount = self._buy_amount(page.url) or self._last_cart_total
        # 金额上限护栏(防绕过 create_order 直接提交)
        if self._max_order_amount > 0 and amount:
            try:
                if float(amount) > self._max_order_amount:
                    shot = await self._shot(page)
                    return OrderDraft(stage="blocked_amount_limit", total_amount=amount,
                                      within_limit=False, shop=str(index), url=page.url,
                                      screenshot_data_uri=shot,
                                      next_action=(f"金额 ¥{amount} 超过上限 ¥{self._max_order_amount},"
                                                   f"拒绝提交。调高 MCP_TAOBAO_MAX_ORDER_AMOUNT 后重试。"),
                                      platform=self.platform_name)
            except ValueError:
                pass

        if not await self._click_text(page, ["提交订单", "提交并支付", "确认支付", "去支付", "立即支付"]):
            shot = await self._shot(page)
            raise PlatformError(self.platform_name,
                                "未找到『提交订单』按钮(页面改版或需先补充信息)。请用 noVNC 查看确认页。")

        # 等待跳转到支付宝收银台(可能同页跳转,也可能新开一页)
        _PAY_HINTS = ("cashier", "alipay", "counter", "payment", "/pay", "tradepay")
        pay_url, pay_page = "", page
        for _ in range(25):
            await page.wait_for_timeout(600)
            found = False
            for pg in self._context.pages:
                u = pg.url or ""
                if any(k in u for k in _PAY_HINTS):
                    pay_url, pay_page, found = u, pg, True
                    break
            if found:
                break

        shot = await self._shot(pay_page)
        if pay_url:
            stage = "submitted_cashier"
            nxt = ("订单已提交,已到支付宝收银台(未输密码)。请在【已登录的浏览器】"
                   "(服务器用 noVNC http://<服务器IP>:6080/vnc.html)打开此链接完成支付。"
                   "链接与登录态/时效绑定,请勿外发。")
        else:
            stage = "submitted_pending_pay"
            pay_url = pay_page.url
            nxt = ("订单已提交,收银台以弹层/同页展示(无独立链接)。请在【已登录的浏览器】"
                   "(noVNC:6080)当前页完成支付。")
        return OrderDraft(stage=stage, total_amount=amount, within_limit=True,
                          shop=str(index), url=pay_url, screenshot_data_uri=shot,
                          next_action=nxt, platform=self.platform_name)
