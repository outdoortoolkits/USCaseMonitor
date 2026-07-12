FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV DISPLAY=:99
ENV USE_XVFB=1
ENV NO_SANDBOX=1

WORKDIR /app

# 安装系统依赖：Xvfb（虚拟显示器）+ Chromium 依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    libnss3 libnspr4 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdrm2 \
    libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright Chromium 浏览器
RUN python -m playwright install chromium

COPY . .

# 启动 Xvfb 包装脚本
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

CMD ["/entrypoint.sh"]
