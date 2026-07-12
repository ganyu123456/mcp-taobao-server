#!/usr/bin/env bash
# 启动顺序：Xvfb 虚拟显示 → x11vnc → noVNC(websockify) → MCP server
set -e

SCREEN="${MCP_TAOBAO_SCREEN:-1280x1024x24}"
DISP="${DISPLAY:-:99}"
NOVNC_PORT="${MCP_TAOBAO_NOVNC_PORT:-6080}"

mkdir -p "${MCP_TAOBAO_USER_DATA_DIR:-/data/profile}"

# 1) 虚拟显示
Xvfb "$DISP" -screen 0 "$SCREEN" -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
# 等待 Xvfb 就绪(检测 X11 socket,不依赖 x11-utils)
for i in $(seq 1 40); do
    [ -S "/tmp/.X11-unix/X${DISP#:}" ] && break
    sleep 0.25
done

# 2) VNC 服务(连接到 Xvfb 的虚拟屏)
if [ -n "$MCP_TAOBAO_VNC_PASSWORD" ]; then
    mkdir -p /root/.vnc
    x11vnc -storepasswd "$MCP_TAOBAO_VNC_PASSWORD" /root/.vnc/passwd >/dev/null 2>&1 || true
    x11vnc -display "$DISP" -rfbauth /root/.vnc/passwd -forever -shared -bg -rfbport 5900 >/tmp/x11vnc.log 2>&1 || true
else
    x11vnc -display "$DISP" -nopw -forever -shared -bg -rfbport 5900 >/tmp/x11vnc.log 2>&1 || true
fi

# 3) noVNC 网页端(浏览器访问 http://<ip>:${NOVNC_PORT}/vnc.html 接管付款)
if [ -d /usr/share/novnc ]; then
    websockify --web=/usr/share/novnc "$NOVNC_PORT" localhost:5900 >/tmp/novnc.log 2>&1 &
fi

export DISPLAY="$DISP"

# 4) MCP server(前台)
exec python -m mcp_taobao_server.server
