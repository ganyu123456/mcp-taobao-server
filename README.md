# MCP Taobao Server（淘宝闪购 / 饿了么外卖）

为大模型提供**淘宝闪购点外卖**能力的 MCP 服务器:**搜附近店 → 看菜单 → 加入购物车 → 生成待支付订单**。
下单类工具会走到「确认订单/支付页」就**停住,绝不自动付款**;登录与付款都由**你人工经 noVNC 完成**。

> 说明:淘宝闪购/外卖没有面向个人的下单开放 API。本 server 通过一个**持久化的有头 Chromium**
> 自动化**饿了么 H5**(`h5.ele.me`);部署在**无屏 Linux 服务器**时用 **Xvfb** 提供虚拟显示,
> 人工操作(登录的短信/滑块、最终付款)通过 **noVNC** 远程接管虚拟桌面完成。
>
> ⚠️ **登录态是会话级**:服务进程存活期间有效,进程重启需重新登录一次。因此以**单一常驻进程**运行。

## 功能

- **登录(人工)**:`shangou_open_login` 打开饿了么登录页(手机号+短信验证码+滑块),由人经 noVNC 完成;`shangou_check_login` 查登录态
- **收货地址**:`shangou_list_addresses` 列常用地址,`shangou_set_address` 选定地址(决定配送范围)
- **搜附近店**:`shangou_search`(奶茶/汉堡/超市/水果…)
- **店内菜单**:`shangou_shop_menu`
- **加入购物车**:`shangou_add_to_cart`
- **查看购物车**:`shangou_view_cart`
- **生成待支付订单**:`shangou_create_order`——走到支付页即停,返回金额+截图+人工付款指引,**含金额上限护栏**
- **提交并取支付链接**:`shangou_submit_order`——点『提交订单』进入支付宝收银台即停(**不代付**),返回收银台链接,人工点链接付款
- 支持 stdio 与 SSE 两种传输

> **成熟度**:登录 + 收货地址 + **搜店/进店/加购/店内搜/结算/提交**已按真实页面实测校正
> (饿了么 H5 为 tiga 影子DOM + 淘宝闪购 `newretail` 页,用文本/`aria-label`/坐标 tap 驱动)。
> 触发滑块/风控时工具返回结构化提示,请用 noVNC 人工处理。

## 安全护栏

- **绝不自动付款/自动登录**:登录与付款都是人工点击(noVNC)。
- **金额上限**:`MCP_TAOBAO_MAX_ORDER_AMOUNT`(默认 100 元),确认页金额超限则拒绝提交并提示。
- **触发风控**:检测到滑块/验证即停并提示人工处理,不盲目重试。

## 快速开始

### 1. 本地开发运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[sse]"
playwright install chromium

# stdio
mcp-taobao-server
# SSE
MCP_TRANSPORT=sse MCP_PORT=8094 mcp-taobao-server
```

### 2. 点外卖流程

1. `shangou_open_login` → 经 noVNC 用手机+短信+滑块登录
2. `shangou_list_addresses` → `shangou_set_address` 选收货地址(**决定配送城市/范围,务必先选对**)
3. `shangou_search` 搜店 → `shangou_shop_menu` 看菜 → `shangou_add_to_cart` 加购(达起送才能结算)
4. `shangou_create_order` → 走到确认页,返回**实付金额**、截图(**未提交、未付款**)
5. 核对无误后二选一付款:
   - `shangou_submit_order` → 提交订单,返回**支付宝收银台链接**,在已登录浏览器(noVNC:6080)点链接付款
   - 或直接在 noVNC(`http://<服务器IP>:6080/vnc.html`)里手动点『提交订单』并付款

## 部署

```bash
cp .env.example .env      # 按需改默认定位、金额上限、VNC 密码
docker compose up -d
```

- 镜像基于 `python:3.11-slim`,内含 **Chromium + Xvfb + x11vnc + noVNC + 精简中文字体**,体积约 **1.2GB**。
- 暴露端口:**8094**(MCP SSE)、**6080**(noVNC 网页,人工登录/付款接管)。
- 登录态卷 `taobao_profile:/data/profile` 持久化 cookie(注意饿了么登录态仍是会话级)。
- 推送 `v*` tag 触发 GitHub Actions:amd64 + arm64 原生构建、推 Harbor、多架构 manifest、GitHub Release。
  需仓库 secrets `HARBOR_USERNAME` / `HARBOR_PASSWORD`。

## MCP 客户端配置

SSE:

```json
{ "mcpServers": { "shangou": { "url": "http://<your-server>:8094/sse" } } }
```

stdio:

```json
{
  "mcpServers": {
    "shangou": {
      "command": "mcp-taobao-server",
      "env": {
        "MCP_TAOBAO_USER_DATA_DIR": "./profile",
        "MCP_TAOBAO_LAT": "32.06",
        "MCP_TAOBAO_LNG": "118.80",
        "MCP_TAOBAO_MAX_ORDER_AMOUNT": "100"
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
| `MCP_TAOBAO_USER_DATA_DIR` | `./profile` | 浏览器持久化 profile |
| `MCP_TAOBAO_HEADLESS` | `false` | 有头(配 Xvfb)/无头 |
| `MCP_TAOBAO_LAT` / `MCP_TAOBAO_LNG` | `32.06` / `118.80` | 默认定位(决定“附近”范围;选地址后以地址为准) |
| `MCP_TAOBAO_TIMEOUT` | `30` | 页面操作超时(秒) |
| `MCP_TAOBAO_MAX_RESULTS` | `10` | 搜索/菜单返回条数上限 |
| `MCP_TAOBAO_MAX_ORDER_AMOUNT` | `100` | 下单金额上限(元),0=不限制 |
| `MCP_TAOBAO_SLOWMO` | `0` | 每步操作放慢(毫秒) |
| `DISPLAY` | `:99` | Xvfb 虚拟显示编号(容器内) |
| `MCP_TAOBAO_SCREEN` | `1280x1024x24` | 虚拟屏分辨率 |
| `MCP_TAOBAO_NOVNC_PORT` | `6080` | noVNC 网页端口 |
| `MCP_TAOBAO_VNC_PASSWORD` | (空) | VNC 密码,空=无密码(建议放内网/反代后) |

## MCP 工具列表

| 工具 | 说明 | 是否需登录 |
|------|------|:--:|
| `shangou_open_login` | 打开饿了么登录页(人工经 noVNC 完成) | 否(用于登录) |
| `shangou_check_login` | 查询登录态 | 否 |
| `shangou_list_addresses` | 列出常用收货地址 | 是 |
| `shangou_set_address` | 选定收货地址 | 是 |
| `shangou_search` | 搜附近店/美食 | 是 |
| `shangou_shop_menu` | 查看店内菜单 | 是 |
| `shangou_add_to_cart` | 加入购物车 | 是 |
| `shangou_view_cart` | 查看购物车 | 是 |
| `shangou_create_order` | 生成待支付订单(停在支付前) | 是 |
| `shangou_submit_order` | **提交订单**并返回支付宝收银台链接(停在收银台,不代付) | 是 |
| `shangou_get_server_status` | 配置与可用性自检 | 否 |

## 项目结构

```
04-mcp-taobao-server/
├── Dockerfile / docker-compose.yaml / entrypoint.sh / .env.example
├── pyproject.toml / requirements.txt
├── .github/workflows/build-release.yaml
└── src/mcp_taobao_server/
    ├── server.py                 # 入口 + shangou_* 工具 + stdio/sse
    └── platforms/
        ├── base.py               # BasePlatform / 结果 dataclass / PlatformError
        └── eleme.py              # 饿了么 H5 Playwright 驱动(登录/地址/搜店/加购/下单停在支付前)
```

## 许可

MIT
