#!/bin/bash
set -e

# 启动 Xvfb 虚拟显示器
Xvfb :99 -screen 0 1280x1024x24 &
XVFB_PID=$!
echo "Xvfb started with PID $XVFB_PID"

# 清理函数
cleanup() {
    echo "Shutting down..."
    kill $XVFB_PID 2>/dev/null || true
}
trap cleanup EXIT

# 启动 Web 服务
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
