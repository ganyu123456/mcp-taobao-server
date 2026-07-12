FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.11-slim
WORKDIR /app

# 运行期系统依赖：Xvfb 虚拟显示 + x11vnc + noVNC(远程接管付款) + 中文字体
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc x11-utils novnc websockify \
        fonts-noto-cjk ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

COPY src/ ./src/
COPY pyproject.toml ./
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && pip install --no-cache-dir -e ".[sse]"

# 安装 Chromium 及其系统依赖(Playwright)
RUN playwright install --with-deps chromium

ENV MCP_TRANSPORT=sse \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8094 \
    MCP_DEFAULT_PLATFORM=taobao \
    MCP_TAOBAO_HEADLESS=false \
    MCP_TAOBAO_USER_DATA_DIR=/data/profile \
    MCP_TAOBAO_SCREEN=1280x1024x24 \
    MCP_TAOBAO_NOVNC_PORT=6080 \
    DISPLAY=:99

# 8094 = MCP SSE；6080 = noVNC 网页(人工付款接管)
EXPOSE 8094 6080

ENTRYPOINT ["/app/entrypoint.sh"]
