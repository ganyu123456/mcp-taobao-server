# MCP Taobao Server

为大模型提供**淘宝购物**能力的 MCP 服务器:**搜索商品 → 加入购物车 → 生成待支付订单**。
下单类工具会走到「确认订单/支付页」就**停住,绝不自动付款**——付款由**你人工完成**。

> 设计取舍:淘宝没有面向个人的下单开放 API,交易+支付+风控不对个人放开。因此本 server 通过
> 一个**持久化的有头 Chromium** 自动化淘宝 H5;部署在**无屏幕的 Linux 服务器**时用 **Xvfb**
> 提供虚拟显示(比 headless 更不易被风控识别),人工付款通过 **noVNC** 远程接管虚拟桌面点击完成。
> 登录用**扫码**(二维码 base64 返回,手机淘宝 App 扫),登录态持久化,一次登录长期复用。

## 功能

- **扫码登录**:`taobao_get_login_qrcode` 返回二维码,手机淘宝扫码;`taobao_check_login` 查登录态
- **搜索商品**:`taobao_search`(标题/价格/商品ID/URL)
- **商品详情**:`taobao_get_item_detail`(价格/销量/规格SKU/主图)
- **加入购物车**:`taobao_add_to_cart`(可选规格与数量,含数量上限护栏)
- **查看购物车**:`taobao_view_cart`
- **生成待支付订单**:`taobao_create_order`——走到支付页即停,返回金额+截图+人工付款指引,**含金额上限护栏**
- 支持 stdio 与 SSE 两种传输

> ⚠️ **成熟度说明**:淘宝 DOM 高度混淆且频繁改版,并有滑块/风控。本项目的页面选择器是
> **初版最佳猜测**(源码中标 `TODO(selectors)`),首次扫码登录后**大概率需要按真实页面校正**才能稳定
> 搜索/加购/下单。触发验证时工具会返回结构化提示,请用 noVNC 人工通过验证。

## 安全护栏

- **绝不自动付款**:下单只走到确认页,付款一定是人工点击。
- **金额上限**:`MCP_TAOBAO_MAX_ORDER_AMOUNT`(默认 200 元),确认页金额超限则拒绝提交并提示。
- **数量上限**:`MCP_TAOBAO_MAX_QTY`(默认 10)。
- **触发风控**:检测到滑块/验证时停止并提示人工经 noVNC 处理,不盲目重试。

## 快速开始

### 1. 本地开发运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[sse]"
playwright install chromium          # 下载 Chromium

# stdio(本地 MCP 客户端)
mcp-taobao-server

# SSE(远程 MCP 客户端)
MCP_TRANSPORT=sse MCP_PORT=8094 mcp-taobao-server
```

本地(有真实显示器)可直接有头运行;无显示器则见下方容器化(Xvfb)。

### 2. 登录与下单流程

1. 调 `taobao_get_login_qrcode` → 用**手机淘宝 App 扫码**登录
2. `taobao_search` 搜菜 → `taobao_add_to_cart` 加购(或 `taobao_create_order` 直接买某件)
3. `taobao_create_order` → 返回订单金额、明细、支付页截图(**未付款**)
4. 浏览器打开 `http://<服务器IP>:6080/vnc.html`(noVNC)→ 在 Chromium 里**手动点击提交并付款**

## 部署

```bash
cp .env.example .env      # 按需改金额上限、站点、VNC 密码
docker compose up -d
```

- 镜像基于 `python:3.11-slim`,内含 **Chromium + Xvfb + x11vnc + noVNC + 精简中文字体(wqy-microhei)**,
  因带浏览器与桌面组件,体积约 **1.2GB**(已做单层安装 + 清缓存瘦身)。
- 暴露端口:**8094**(MCP SSE)、**6080**(noVNC 网页,人工付款接管)。
- 登录态通过卷 `taobao_profile:/data/profile` 持久化,重启不丢登录。
- 推送 `v*` tag 触发 GitHub Actions:amd64 + arm64 原生构建、推 Harbor、多架构 manifest、GitHub Release。
  需配置仓库 secrets `HARBOR_USERNAME` / `HARBOR_PASSWORD`。

## MCP 客户端配置

SSE:

```json
{ "mcpServers": { "taobao": { "url": "http://<your-server>:8094/sse" } } }
```

stdio:

```json
{
  "mcpServers": {
    "taobao": {
      "command": "mcp-taobao-server",
      "env": {
        "MCP_TAOBAO_USER_DATA_DIR": "./profile",
        "MCP_TAOBAO_HEADLESS": "false",
        "MCP_TAOBAO_MAX_ORDER_AMOUNT": "200"
      }
    }
  }
}
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MCP_TRANSPORT` | `stdio` | `stdio` 或 `sse` |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8094` | SSE 监听地址 |
| `MCP_TAOBAO_USER_DATA_DIR` | `./profile` | 浏览器持久化 profile(登录态) |
| `MCP_TAOBAO_HEADLESS` | `false` | 有头(配 Xvfb)/无头;下单需有头 |
| `MCP_TAOBAO_SITE` | `h5` | `h5`(m.taobao.com,反爬弱)/`pc` |
| `MCP_TAOBAO_TIMEOUT` | `30` | 页面操作超时(秒) |
| `MCP_TAOBAO_MAX_RESULTS` | `10` | 搜索返回条数上限 |
| `MCP_TAOBAO_MAX_ORDER_AMOUNT` | `200` | 下单金额上限(元),0=不限制 |
| `MCP_TAOBAO_MAX_QTY` | `10` | 单次加购最大数量 |
| `MCP_TAOBAO_SLOWMO` | `0` | 每步操作放慢(毫秒) |
| `DISPLAY` | `:99` | Xvfb 虚拟显示编号(容器内) |
| `MCP_TAOBAO_SCREEN` | `1280x1024x24` | 虚拟屏分辨率 |
| `MCP_TAOBAO_NOVNC_PORT` | `6080` | noVNC 网页端口 |
| `MCP_TAOBAO_VNC_PASSWORD` | (空) | VNC 密码,空=无密码(建议放内网/反代后) |

## MCP 工具列表

| 工具 | 说明 | 是否需登录 |
|------|------|:--:|
| `taobao_get_login_qrcode` | 获取扫码登录二维码 | 否(用于登录) |
| `taobao_check_login` | 查询登录态 | 否 |
| `taobao_search` | 搜索商品 | 建议 |
| `taobao_get_item_detail` | 商品详情 | 建议 |
| `taobao_add_to_cart` | 加入购物车 | 是 |
| `taobao_view_cart` | 查看购物车 | 是 |
| `taobao_create_order` | 生成待支付订单(停在支付前) | 是 |
| `taobao_get_server_status` | 配置与可用性自检 | 否 |

## 项目结构

```
04-mcp-taobao-server/
├── Dockerfile / docker-compose.yaml / entrypoint.sh / .env.example
├── pyproject.toml / requirements.txt
├── .github/workflows/build-release.yaml
└── src/mcp_taobao_server/
    ├── server.py                 # 入口 + 8 个工具 + stdio/sse
    └── platforms/
        ├── base.py               # BasePlatform / 结果 dataclass / PlatformError
        └── taobao.py             # Playwright 持久化浏览器驱动(搜索/加购/下单停在支付前)
```

## 许可

MIT
