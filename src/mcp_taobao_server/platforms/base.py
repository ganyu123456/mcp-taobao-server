"""Abstraction layer for Taobao automation backends.

Follows the project convention: result dataclasses (each with ``to_dict``) +
an ABC base class + a domain-specific error type. A backend drives a real
browser (Playwright) rather than a stateless HTTP API, so the base class also
declares ``start`` / ``close`` lifecycle hooks for the persistent context.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Result dataclasses (flat, JSON-serialisable)
# ---------------------------------------------------------------------------

@dataclass
class LoginStatus:
    """Whether the persistent browser context is logged in to Taobao."""
    logged_in: bool = False
    nick: str = ""            # account nickname when logged in
    platform: str = ""

    def to_dict(self) -> dict:
        return {"logged_in": self.logged_in, "nick": self.nick, "platform": self.platform}


@dataclass
class LoginQrcode:
    """Login QR code to be scanned by the Taobao mobile app."""
    qrcode_data_uri: str = ""   # data:image/png;base64,... of the QR image
    tip: str = ""
    platform: str = ""

    def to_dict(self) -> dict:
        return {"qrcode_data_uri": self.qrcode_data_uri, "tip": self.tip, "platform": self.platform}


@dataclass
class Item:
    """A single search-result item."""
    item_id: str = ""
    title: str = ""
    price: str = ""
    shop: str = ""
    location: str = ""
    sales: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id, "title": self.title, "price": self.price,
            "shop": self.shop, "location": self.location, "sales": self.sales, "url": self.url,
        }


@dataclass
class SearchResult:
    keyword: str = ""
    count: int = 0
    items: list = field(default_factory=list)   # list[Item]
    platform: str = ""

    def to_dict(self) -> dict:
        return {
            "keyword": self.keyword, "count": self.count,
            "items": [i.to_dict() for i in self.items], "platform": self.platform,
        }


@dataclass
class ItemDetail:
    item_id: str = ""
    title: str = ""
    price: str = ""
    shop: str = ""
    sales: str = ""
    skus: list = field(default_factory=list)    # list[str] of sku option labels
    images: list = field(default_factory=list)  # list[str] of image urls
    url: str = ""

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id, "title": self.title, "price": self.price,
            "shop": self.shop, "sales": self.sales, "skus": self.skus,
            "images": self.images, "url": self.url,
        }


@dataclass
class CartLine:
    title: str = ""
    price: str = ""
    quantity: int = 0
    sku: str = ""
    checked: bool = False

    def to_dict(self) -> dict:
        return {
            "title": self.title, "price": self.price, "quantity": self.quantity,
            "sku": self.sku, "checked": self.checked,
        }


@dataclass
class ActionResult:
    """Generic result for a write action (add to cart / etc.)."""
    ok: bool = False
    message: str = ""
    url: str = ""
    screenshot_data_uri: str = ""
    platform: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "message": self.message, "url": self.url,
            "screenshot_data_uri": self.screenshot_data_uri, "platform": self.platform,
        }


@dataclass
class CartView:
    count: int = 0
    lines: list = field(default_factory=list)   # list[CartLine]
    url: str = ""
    platform: str = ""

    def to_dict(self) -> dict:
        return {
            "count": self.count, "lines": [l.to_dict() for l in self.lines],
            "url": self.url, "platform": self.platform,
        }


@dataclass
class OrderDraft:
    """A confirm-order page reached but NOT paid. Human pays manually."""
    stage: str = ""              # e.g. "confirm_order" / "blocked_amount_limit"
    total_amount: str = ""       # parsed total, yuan
    within_limit: bool = True
    lines: list = field(default_factory=list)    # list[CartLine]
    url: str = ""
    screenshot_data_uri: str = ""
    next_action: str = ""        # human instruction (e.g. noVNC 接管付款)
    platform: str = ""

    def to_dict(self) -> dict:
        return {
            "stage": self.stage, "total_amount": self.total_amount,
            "within_limit": self.within_limit,
            "lines": [l.to_dict() for l in self.lines], "url": self.url,
            "screenshot_data_uri": self.screenshot_data_uri,
            "next_action": self.next_action, "platform": self.platform,
        }


# ---------------------------------------------------------------------------
# Base backend
# ---------------------------------------------------------------------------

class BasePlatform(ABC):
    @property
    @abstractmethod
    def platform_name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool:
        """Config is complete (playwright importable, profile dir writable).

        Does not launch the browser or hit the network.
        """
        ...

    # ---- lifecycle (persistent browser context) ----
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # ---- login ----
    @abstractmethod
    async def check_login(self) -> LoginStatus: ...

    @abstractmethod
    async def get_login_qrcode(self) -> LoginQrcode: ...

    # ---- browse ----
    @abstractmethod
    async def search(self, keyword: str, limit: int) -> SearchResult: ...

    @abstractmethod
    async def get_item_detail(self, item: str) -> ItemDetail: ...

    # ---- cart / order (order stops before payment) ----
    @abstractmethod
    async def add_to_cart(self, item: str, quantity: int, sku: str) -> ActionResult: ...

    @abstractmethod
    async def view_cart(self) -> CartView: ...

    @abstractmethod
    async def create_order(self, item: Optional[str], quantity: int, sku: str) -> OrderDraft: ...


class PlatformError(Exception):
    def __init__(self, platform: str, message: str):
        self.platform = platform
        self.message = message
        super().__init__(f"[{platform}] {message}")
