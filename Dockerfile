FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.11-slim
WORKDIR /app

# 运行期系统依赖：Xvfb 虚拟显示 + x11vnc + noVNC(远程接管付款) + 中文字体
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

COPY src/ ./src/
COPY pyproject.toml ./
COPY entrypoint.sh /app/entrypoint.sh

# 单层安装：本包 + Xvfb/VNC 组件 + 极小中文字体 + Chromium 依赖与浏览器，
# 末尾统一清理 apt/pip 缓存与 Playwright 附带的 ffmpeg，尽量缩小镜像。
RUN chmod +x /app/entrypoint.sh \
    && pip install --no-cache-dir -e ".[sse]" \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         xvfb x11vnc novnc websockify fonts-wqy-microhei ca-certificates \
    && playwright install-deps chromium \
    && playwright install chromium \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip /root/.cache/ms-playwright/ffmpeg-*

ENV MCP_TRANSPORT=sse \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8094 \
    MCP_DEFAULT_PLATFORM=eleme \
    MCP_TAOBAO_HEADLESS=false \
    MCP_TAOBAO_USER_DATA_DIR=/data/profile \
    MCP_TAOBAO_LAT=32.06 \
    MCP_TAOBAO_LNG=118.80 \
    MCP_TAOBAO_SCREEN=1280x1024x24 \
    MCP_TAOBAO_NOVNC_PORT=6080 \
    DISPLAY=:99

# 8094 = MCP SSE；6080 = noVNC 网页(人工付款接管)
EXPOSE 8094 6080

ENTRYPOINT ["/app/entrypoint.sh"]
