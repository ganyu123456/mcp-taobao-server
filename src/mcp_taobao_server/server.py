#!/usr/bin/env python3
"""MCP Taobao Server - 淘宝闪购/饿了么外卖(浏览器自动化,人工付款).

Usage:
    mcp-taobao-server                    # stdio(默认)
    MCP_TRANSPORT=sse mcp-taobao-server  # SSE

设计:通过一个持久化的有头 Chromium(Linux 服务器用 Xvfb 虚拟显示)操作饿了么 H5。
登录(手机+短信+滑块)与最终付款都由人经 noVNC 接管虚拟桌面完成;下单类工具走到
"确认订单/支付页"就停,绝不自动付款。
"""

import asyncio
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent

from .platforms.base import PlatformError
from .platforms.eleme import ElemePlatform

# ---- 1) 环境变量在模块顶层读取一次 ----
load_dotenv()


def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


USER_DATA_DIR = os.getenv("MCP_TAOBAO_USER_DATA_DIR", "./profile")
HEADLESS = _as_bool(os.getenv("MCP_TAOBAO_HEADLESS", "false"))
TIMEOUT = int(os.getenv("MCP_TAOBAO_TIMEOUT", "30"))
LAT = float(os.getenv("MCP_TAOBAO_LAT", "32.06"))
LNG = float(os.getenv("MCP_TAOBAO_LNG", "118.80"))
MAX_RESULTS = int(os.getenv("MCP_TAOBAO_MAX_RESULTS", "10"))
MAX_ORDER_AMOUNT = float(os.getenv("MCP_TAOBAO_MAX_ORDER_AMOUNT", "100"))
SLOWMO = int(os.getenv("MCP_TAOBAO_SLOWMO", "0"))

ENABLED = [p.strip() for p in os.getenv("MCP_ENABLED_PLATFORMS", "eleme").split(",") if p.strip()]
DEFAULT_PLATFORM = os.getenv("MCP_DEFAULT_PLATFORM", "eleme").strip()

# ---- 2) 全局 Server 实例 ----
server = Server("mcp-taobao-server")

_platforms: dict[str, Any] = {}


# ---- 3) 工厂:构造后端失败不崩溃 ----
def _init_platforms():
    global _platforms
    _platforms = {}
    platform_map = {
        "eleme": lambda: ElemePlatform(
            user_data_dir=USER_DATA_DIR,
            headless=HEADLESS,
            timeout=TIMEOUT,
            lat=LAT,
            lng=LNG,
            max_results=MAX_RESULTS,
            max_order_amount=MAX_ORDER_AMOUNT,
            slowmo=SLOWMO,
        ),
    }
    for name in ENABLED:
        if name in platform_map:
            try:
                _platforms[name] = platform_map[name]()
            except Exception:
                pass


def _get_platform(name: str):
    p = _platforms.get(name)
    if p is None:
        raise PlatformError(name, "Platform is not enabled.")
    if not p.is_available():
        raise PlatformError(name, "Platform 不可用:请确认已安装 playwright chromium 且 profile 目录可写。")
    return p


# ---- 4) 工具清单 ----
@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="shangou_check_login",
            description="检查是否已登录饿了么(淘宝闪购外卖)。未登录请调 shangou_open_login,并在 noVNC 里人工完成手机+短信+滑块登录。",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="shangou_open_login",
            description="打开饿了么登录页(手机号+短信验证码+滑块)。因含短信与滑块验证,须由人在 noVNC(http://<服务器IP>:6080/vnc.html) 的浏览器窗口里完成;返回当前页截图与操作指引。登录态在服务进程存活期间有效。",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="shangou_list_addresses",
            description="列出账号里的常用收货地址(index/名称/联系人)。下单前需先用 shangou_set_address 选定一个地址。",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="shangou_set_address",
            description="选择收货地址以确定配送范围。参数 keyword=地址关键词(如'马家店春华园'),或 index=shangou_list_addresses 里的序号(从0起)。选好后即可搜附近店。",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "地址关键词(可选)", "default": ""},
                    "index": {"type": "integer", "description": "地址序号,从0起(可选)", "default": 0},
                },
            },
        ),
        Tool(
            name="shangou_search",
            description="搜索附近可配送的店铺/美食。参数 keyword=关键词(如'奶茶'/'汉堡'/'超市'/'水果');limit=返回条数。需先登录并设好收货地址,否则返回结构化错误。",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "description": "返回条数,默认10", "default": 10},
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="shangou_shop_menu",
            description="查看某店铺的菜单/商品。参数 shop=店铺ID或店铺URL;limit=返回条数。返回菜品名称与价格(尽力解析)。",
            inputSchema={
                "type": "object",
                "properties": {
                    "shop": {"type": "string", "description": "店铺ID或URL"},
                    "limit": {"type": "integer", "description": "返回条数,默认10", "default": 10},
                },
                "required": ["shop"],
            },
        ),
        Tool(
            name="shangou_add_to_cart",
            description="把某店铺的菜品加入购物车。参数 shop=店铺ID/URL;item=菜品名称;quantity=份数(默认1)。可能需先选规格,返回是否成功及截图。",
            inputSchema={
                "type": "object",
                "properties": {
                    "shop": {"type": "string", "description": "店铺ID或URL"},
                    "item": {"type": "string", "description": "菜品名称"},
                    "quantity": {"type": "integer", "description": "份数,默认1", "default": 1},
                },
                "required": ["shop", "item"],
            },
        ),
        Tool(
            name="shangou_view_cart",
            description="查看某店铺当前购物车内容(尽力解析菜品与价格)。参数 shop=店铺ID/URL。",
            inputSchema={
                "type": "object",
                "properties": {
                    "shop": {"type": "string", "description": "店铺ID或URL"},
                },
                "required": ["shop"],
            },
        ),
        Tool(
            name="shangou_create_order",
            description=(
                "生成待支付外卖订单:点『去结算』走到确认订单/支付页即【停止,绝不提交付款】,返回金额、页面截图与人工付款指引。"
                "参数 shop=店铺ID/URL(需已在该店加购)。金额超过服务端上限(MCP_TAOBAO_MAX_ORDER_AMOUNT)会被拦截。"
                "付款请经 noVNC(:6080)在浏览器手动完成。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "shop": {"type": "string", "description": "店铺ID或URL"},
                },
                "required": ["shop"],
            },
        ),
        Tool(
            name="shangou_get_server_status",
            description="查询服务配置与可用性(是否装好 playwright、profile 目录、有头/无头、默认定位、金额上限等)。无需登录即可调用。",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ---- 5) 统一分发 + 异常兜底 ----
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        result = await _handle_tool(name, arguments)
        return [TextContent(type="text", text=result)]
    except PlatformError as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "platform": e.platform}, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e)}, ensure_ascii=False))]


async def _handle_tool(name: str, args: dict[str, Any]) -> str:
    if name == "shangou_check_login":
        p = _get_platform(DEFAULT_PLATFORM)
        return json.dumps((await p.check_login()).to_dict(), ensure_ascii=False)
    if name == "shangou_open_login":
        p = _get_platform(DEFAULT_PLATFORM)
        return json.dumps((await p.open_login()).to_dict(), ensure_ascii=False)
    if name == "shangou_list_addresses":
        p = _get_platform(DEFAULT_PLATFORM)
        return json.dumps((await p.list_addresses()).to_dict(), ensure_ascii=False)
    if name == "shangou_set_address":
        p = _get_platform(DEFAULT_PLATFORM)
        keyword = str(args.get("keyword", "") or "").strip()
        index = int(args.get("index", 0) or 0)
        return json.dumps((await p.set_address(keyword, index)).to_dict(), ensure_ascii=False)
    if name == "shangou_search":
        p = _get_platform(DEFAULT_PLATFORM)
        keyword = str(args.get("keyword", "")).strip()
        if not keyword:
            raise PlatformError(DEFAULT_PLATFORM, "keyword 不能为空。")
        limit = int(args.get("limit", MAX_RESULTS) or MAX_RESULTS)
        return json.dumps((await p.search(keyword, limit)).to_dict(), ensure_ascii=False)
    if name == "shangou_shop_menu":
        p = _get_platform(DEFAULT_PLATFORM)
        shop = str(args.get("shop", "")).strip()
        if not shop:
            raise PlatformError(DEFAULT_PLATFORM, "shop 不能为空。")
        limit = int(args.get("limit", MAX_RESULTS) or MAX_RESULTS)
        return json.dumps((await p.shop_menu(shop, limit)).to_dict(), ensure_ascii=False)
    if name == "shangou_add_to_cart":
        p = _get_platform(DEFAULT_PLATFORM)
        shop = str(args.get("shop", "")).strip()
        item = str(args.get("item", "")).strip()
        if not shop or not item:
            raise PlatformError(DEFAULT_PLATFORM, "shop 与 item 均不能为空。")
        qty = int(args.get("quantity", 1) or 1)
        return json.dumps((await p.add_to_cart(shop, item, qty)).to_dict(), ensure_ascii=False)
    if name == "shangou_view_cart":
        p = _get_platform(DEFAULT_PLATFORM)
        shop = str(args.get("shop", "")).strip()
        if not shop:
            raise PlatformError(DEFAULT_PLATFORM, "shop 不能为空。")
        return json.dumps((await p.view_cart(shop)).to_dict(), ensure_ascii=False)
    if name == "shangou_create_order":
        p = _get_platform(DEFAULT_PLATFORM)
        shop = str(args.get("shop", "")).strip()
        if not shop:
            raise PlatformError(DEFAULT_PLATFORM, "shop 不能为空。")
        return json.dumps((await p.create_order(shop)).to_dict(), ensure_ascii=False)
    if name == "shangou_get_server_status":
        return _server_status()
    return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)


def _server_status() -> str:
    p = _platforms.get(DEFAULT_PLATFORM)
    available = bool(p and p.is_available())
    status = {
        "platform": DEFAULT_PLATFORM,
        "enabled": p is not None,
        "available": available,
        "playwright_installed": _playwright_installed(),
        "headless": HEADLESS,
        "default_geo": {"lat": LAT, "lng": LNG},
        "user_data_dir": os.path.abspath(os.path.expanduser(USER_DATA_DIR)),
        "max_order_amount": MAX_ORDER_AMOUNT,
        "note": "登录(手机+短信+滑块)与付款均由人经 noVNC(:6080)完成;下单类工具停在支付前。",
    }
    return json.dumps(status, ensure_ascii=False)


def _playwright_installed() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


# ---- 6) 传输层:stdio + SSE ----
async def main():
    _init_platforms()
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    try:
        if transport == "sse":
            await _run_sse()
        else:
            await _run_stdio()
    finally:
        p = _platforms.get(DEFAULT_PLATFORM)
        if p is not None:
            try:
                await p.close()
            except Exception:
                pass


async def _run_stdio():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def _run_sse():
    try:
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Mount, Route as StarletteRoute
        import uvicorn
    except ImportError:
        print("[mcp-taobao-server] SSE requires: pip install mcp-taobao-server[sse]", file=sys.stderr)
        sys.exit(1)

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8094"))
    transport_instance = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with transport_instance.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
        return Response()   # 关键:必须返回 Response()

    app = Starlette(routes=[
        StarletteRoute("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=transport_instance.handle_post_message),  # 关键:Mount,不是 Route
    ])
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
