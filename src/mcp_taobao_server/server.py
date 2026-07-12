#!/usr/bin/env python3
"""MCP Taobao Server - 淘宝商品搜索/加购/生成待支付订单(浏览器自动化,人工付款).

Usage:
    mcp-taobao-server                    # stdio(默认)
    MCP_TRANSPORT=sse mcp-taobao-server  # SSE

设计:通过一个持久化的有头 Chromium(Linux 服务器用 Xvfb 虚拟显示)操作淘宝 H5。
下单类工具会走到"确认订单/支付页"就停,付款由人工通过 noVNC 接管虚拟桌面完成。
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
from .platforms.taobao import TaobaoPlatform

# ---- 1) 环境变量在模块顶层读取一次 ----
load_dotenv()


def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


USER_DATA_DIR = os.getenv("MCP_TAOBAO_USER_DATA_DIR", "./profile")
HEADLESS = _as_bool(os.getenv("MCP_TAOBAO_HEADLESS", "false"))
SITE = os.getenv("MCP_TAOBAO_SITE", "h5").strip()
TIMEOUT = int(os.getenv("MCP_TAOBAO_TIMEOUT", "30"))
MAX_RESULTS = int(os.getenv("MCP_TAOBAO_MAX_RESULTS", "10"))
MAX_ORDER_AMOUNT = float(os.getenv("MCP_TAOBAO_MAX_ORDER_AMOUNT", "200"))
MAX_QTY = int(os.getenv("MCP_TAOBAO_MAX_QTY", "10"))
SLOWMO = int(os.getenv("MCP_TAOBAO_SLOWMO", "0"))

ENABLED = [p.strip() for p in os.getenv("MCP_ENABLED_PLATFORMS", "taobao").split(",") if p.strip()]
DEFAULT_PLATFORM = os.getenv("MCP_DEFAULT_PLATFORM", "taobao").strip()

# ---- 2) 全局 Server 实例 ----
server = Server("mcp-taobao-server")

_platforms: dict[str, Any] = {}


# ---- 3) 工厂:构造后端失败不崩溃 ----
def _init_platforms():
    global _platforms
    _platforms = {}
    platform_map = {
        "taobao": lambda: TaobaoPlatform(
            user_data_dir=USER_DATA_DIR,
            headless=HEADLESS,
            site=SITE,
            timeout=TIMEOUT,
            max_results=MAX_RESULTS,
            max_order_amount=MAX_ORDER_AMOUNT,
            max_qty=MAX_QTY,
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
            name="taobao_check_login",
            description="检查当前持久化浏览器是否已登录淘宝。返回 logged_in 与昵称。未登录请先调 taobao_get_login_qrcode。",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="taobao_get_login_qrcode",
            description="获取淘宝扫码登录二维码(返回 PNG 的 data URI)。用手机淘宝 App 扫码后即完成登录,登录态持久化,后续工具可直接使用。若返回的是整页截图或提示滑块,请用 noVNC(:6080) 处理。",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="taobao_search",
            description="在淘宝搜索商品,返回前 N 条(标题/价格/商品ID/URL)。参数 keyword=关键词(如'西红柿 5斤');limit=返回条数(默认10,上限由服务端配置)。未登录或触发风控时会返回结构化错误。",
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
            name="taobao_get_item_detail",
            description="查看商品详情。参数 item=商品ID 或 完整商品URL。返回标题/价格/销量/可选规格SKU/主图。",
            inputSchema={
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "商品ID或商品URL"},
                },
                "required": ["item"],
            },
        ),
        Tool(
            name="taobao_add_to_cart",
            description="把商品加入购物车。参数 item=商品ID或URL;quantity=数量(默认1,上限由服务端配置);sku=规格文案(可选,如'5斤装')。返回是否成功及页面截图。可能需先登录/选规格。",
            inputSchema={
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "商品ID或商品URL"},
                    "quantity": {"type": "integer", "description": "数量,默认1", "default": 1},
                    "sku": {"type": "string", "description": "规格文案(可选)", "default": ""},
                },
                "required": ["item"],
            },
        ),
        Tool(
            name="taobao_view_cart",
            description="查看购物车内容,返回各行商品的标题/价格(尽力解析)。",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="taobao_create_order",
            description=(
                "生成待支付订单:走到'确认订单/支付页'即【停止,绝不提交付款】,返回订单金额、明细、页面截图和人工付款指引。"
                "参数 item=商品ID/URL(直接购买该商品);若省略 item 则从购物车结算。quantity/sku 同加购。"
                "金额超过服务端上限(MCP_TAOBAO_MAX_ORDER_AMOUNT)会被拦截。付款请经 noVNC(:6080)在浏览器手动完成。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "商品ID或URL;省略则从购物车结算", "default": ""},
                    "quantity": {"type": "integer", "description": "数量,默认1", "default": 1},
                    "sku": {"type": "string", "description": "规格文案(可选)", "default": ""},
                },
            },
        ),
        Tool(
            name="taobao_get_server_status",
            description="查询服务配置与可用性(是否装好 playwright、profile 目录、有头/无头、站点、金额上限等)。无需登录即可调用。",
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
    if name == "taobao_check_login":
        p = _get_platform(DEFAULT_PLATFORM)
        return json.dumps((await p.check_login()).to_dict(), ensure_ascii=False)
    if name == "taobao_get_login_qrcode":
        p = _get_platform(DEFAULT_PLATFORM)
        return json.dumps((await p.get_login_qrcode()).to_dict(), ensure_ascii=False)
    if name == "taobao_search":
        p = _get_platform(DEFAULT_PLATFORM)
        keyword = str(args.get("keyword", "")).strip()
        if not keyword:
            raise PlatformError(DEFAULT_PLATFORM, "keyword 不能为空。")
        limit = int(args.get("limit", MAX_RESULTS) or MAX_RESULTS)
        return json.dumps((await p.search(keyword, limit)).to_dict(), ensure_ascii=False)
    if name == "taobao_get_item_detail":
        p = _get_platform(DEFAULT_PLATFORM)
        item = str(args.get("item", "")).strip()
        if not item:
            raise PlatformError(DEFAULT_PLATFORM, "item 不能为空。")
        return json.dumps((await p.get_item_detail(item)).to_dict(), ensure_ascii=False)
    if name == "taobao_add_to_cart":
        p = _get_platform(DEFAULT_PLATFORM)
        item = str(args.get("item", "")).strip()
        if not item:
            raise PlatformError(DEFAULT_PLATFORM, "item 不能为空。")
        qty = int(args.get("quantity", 1) or 1)
        sku = str(args.get("sku", "") or "").strip()
        return json.dumps((await p.add_to_cart(item, qty, sku)).to_dict(), ensure_ascii=False)
    if name == "taobao_view_cart":
        p = _get_platform(DEFAULT_PLATFORM)
        return json.dumps((await p.view_cart()).to_dict(), ensure_ascii=False)
    if name == "taobao_create_order":
        p = _get_platform(DEFAULT_PLATFORM)
        item = str(args.get("item", "") or "").strip() or None
        qty = int(args.get("quantity", 1) or 1)
        sku = str(args.get("sku", "") or "").strip()
        return json.dumps((await p.create_order(item, qty, sku)).to_dict(), ensure_ascii=False)
    if name == "taobao_get_server_status":
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
        "site": SITE,
        "user_data_dir": os.path.abspath(os.path.expanduser(USER_DATA_DIR)),
        "max_order_amount": MAX_ORDER_AMOUNT,
        "max_qty": MAX_QTY,
        "note": "下单类工具停在支付前;付款请经 noVNC(:6080)人工完成。",
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
