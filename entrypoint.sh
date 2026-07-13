#!/usr/bin/env bash
# 启动顺序：Xvfb 虚拟显示 → x11vnc → noVNC(websockify) → MCP server
set -e

SCREEN="${MCP_TAOBAO_SCREEN:-1280x1024x24}"
NOVNC_PORT="${MCP_TAOBAO_NOVNC_PORT:-6080}"

mkdir -p "${MCP_TAOBAO_USER_DATA_DIR:-/data/profile}"

# 0) 清理残留的 Chrome profile 锁(避免 Playwright 报 "Opening in existing browser session")
rm -f "${MCP_TAOBAO_USER_DATA_DIR:-/data/profile}"/Singleton{Lock,Cookie,Socket} 2>/dev/null || true

# 1) 虚拟显示 — 尝试启动 Xvfb,优先用 DISPLAY 环境变量,冲突时自动选空闲编号
_xvfb_start() {
    local dpy_num="${DISPLAY:-:99}"
    dpy_num="${dpy_num#:}"
    # 先清理可能的残留锁文件/套接字
    for _try in $(seq 0 5); do
        local _dn=$((dpy_num + _try))
        rm -f "/tmp/.X${_dn}-lock" "/tmp/.X11-unix/X${_dn}" 2>/dev/null || true
        Xvfb ":${_dn}" -screen 0 "$SCREEN" -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
        # 等待 Xvfb 就绪
        for _i in $(seq 1 20); do
            [ -S "/tmp/.X11-unix/X${_dn}" ] && break
            sleep 0.25
        done
        if [ -S "/tmp/.X11-unix/X${_dn}" ]; then
            DISP=":${_dn}"
            return 0
        fi
        pkill -9 -f "Xvfb :${_dn}" 2>/dev/null || true
    done
    return 1
}
_xvfb_start || { echo "Xvfb 启动失败"; cat /tmp/xvfb.log; exit 1; }

echo "Xvfb started on ${DISP}"

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
