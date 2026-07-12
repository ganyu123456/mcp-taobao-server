"""Abstraction layer for 淘宝闪购/饿了么 takeout automation backends.

Convention: result dataclasses (each with ``to_dict``) + an ABC base class +
a domain error. A backend drives a real browser (Playwright), so the base also
declares ``start`` / ``close`` lifecycle hooks for the persistent context.

Takeout flow (ele.me H5): 登录(手机+短信+滑块) → 选收货地址 → 搜附近店/菜 →
加购 → 生成订单(停在支付前,人工付款)。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LoginStatus:
    logged_in: bool = False
    phone: str = ""
    platform: str = ""

    def to_dict(self) -> dict:
        return {"logged_in": self.logged_in, "phone": self.phone, "platform": self.platform}


@dataclass
class Address:
    index: int = 0
    label: str = ""        # 地址主名(如 马家店春华园2栋)
    detail: str = ""       # 门牌/补充(如 806)
    contact: str = ""      # 联系人+电话
    current: bool = False  # 是否当前选中
    def to_dict(self) -> dict:
        return {"index": self.index, "label": self.label, "detail": self.detail,
                "contact": self.contact, "current": self.current}


@dataclass
class AddressList:
    count: int = 0
    addresses: list = field(default_factory=list)  # list[Address]
    url: str = ""
    platform: str = ""
    def to_dict(self) -> dict:
        return {"count": self.count, "addresses": [a.to_dict() for a in self.addresses],
                "url": self.url, "platform": self.platform}


@dataclass
class Shop:
    shop_id: str = ""
    name: str = ""
    rating: str = ""
    sales: str = ""          # 月售
    delivery_fee: str = ""   # 配送费
    min_order: str = ""      # 起送
    eta: str = ""            # 预计送达
    distance: str = ""
    url: str = ""
    def to_dict(self) -> dict:
        return {"shop_id": self.shop_id, "name": self.name, "rating": self.rating,
                "sales": self.sales, "delivery_fee": self.delivery_fee,
                "min_order": self.min_order, "eta": self.eta, "distance": self.distance, "url": self.url}


@dataclass
class SearchResult:
    keyword: str = ""
    count: int = 0
    shops: list = field(default_factory=list)  # list[Shop]
    url: str = ""
    platform: str = ""
    def to_dict(self) -> dict:
        return {"keyword": self.keyword, "count": self.count,
                "shops": [s.to_dict() for s in self.shops], "url": self.url, "platform": self.platform}


@dataclass
class MenuItem:
    item_id: str = ""
    name: str = ""
    price: str = ""
    desc: str = ""
    sales: str = ""
    def to_dict(self) -> dict:
        return {"item_id": self.item_id, "name": self.name, "price": self.price,
                "desc": self.desc, "sales": self.sales}


@dataclass
class ShopMenu:
    shop_id: str = ""
    shop_name: str = ""
    count: int = 0
    items: list = field(default_factory=list)  # list[MenuItem]
    url: str = ""
    platform: str = ""
    def to_dict(self) -> dict:
        return {"shop_id": self.shop_id, "shop_name": self.shop_name, "count": self.count,
                "items": [i.to_dict() for i in self.items], "url": self.url, "platform": self.platform}


@dataclass
class ActionResult:
    ok: bool = False
    message: str = ""
    url: str = ""
    screenshot_data_uri: str = ""
    platform: str = ""
    def to_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "url": self.url,
                "screenshot_data_uri": self.screenshot_data_uri, "platform": self.platform}


@dataclass
class CartLine:
    name: str = ""
    price: str = ""
    quantity: int = 0
    def to_dict(self) -> dict:
        return {"name": self.name, "price": self.price, "quantity": self.quantity}


@dataclass
class CartView:
    count: int = 0
    lines: list = field(default_factory=list)  # list[CartLine]
    total: str = ""
    url: str = ""
    platform: str = ""
    def to_dict(self) -> dict:
        return {"count": self.count, "lines": [l.to_dict() for l in self.lines],
                "total": self.total, "url": self.url, "platform": self.platform}


@dataclass
class OrderDraft:
    """确认订单/支付页,已到达但未付款。人工经 noVNC 付款。"""
    stage: str = ""              # confirm_order / blocked_amount_limit
    total_amount: str = ""
    within_limit: bool = True
    address: str = ""
    shop: str = ""
    lines: list = field(default_factory=list)  # list[CartLine]
    url: str = ""
    screenshot_data_uri: str = ""
    next_action: str = ""
    platform: str = ""
    def to_dict(self) -> dict:
        return {"stage": self.stage, "total_amount": self.total_amount,
                "within_limit": self.within_limit, "address": self.address, "shop": self.shop,
                "lines": [l.to_dict() for l in self.lines], "url": self.url,
                "screenshot_data_uri": self.screenshot_data_uri,
                "next_action": self.next_action, "platform": self.platform}


class BasePlatform(ABC):
    @property
    @abstractmethod
    def platform_name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # ---- login (手机+短信+滑块,人工经 noVNC 完成) ----
    @abstractmethod
    async def check_login(self) -> LoginStatus: ...

    @abstractmethod
    async def open_login(self) -> ActionResult: ...

    # ---- address ----
    @abstractmethod
    async def list_addresses(self) -> AddressList: ...

    @abstractmethod
    async def set_address(self, keyword: str, index: int) -> ActionResult: ...

    # ---- browse ----
    @abstractmethod
    async def search(self, keyword: str, limit: int) -> SearchResult: ...

    @abstractmethod
    async def shop_menu(self, shop: str, limit: int) -> ShopMenu: ...

    # ---- cart / order (停在支付前) ----
    @abstractmethod
    async def add_to_cart(self, shop: str, item: str, quantity: int) -> ActionResult: ...

    @abstractmethod
    async def view_cart(self, shop: str) -> CartView: ...

    @abstractmethod
    async def create_order(self, shop: str) -> OrderDraft: ...


class PlatformError(Exception):
    def __init__(self, platform: str, message: str):
        self.platform = platform
        self.message = message
        super().__init__(f"[{platform}] {message}")
