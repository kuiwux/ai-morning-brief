#!/bin/bash
# AI 晨报 · 实时轮询守护进程启动脚本
# 用法: bash run_poller.sh

set -e

cd /tmp/ai_morning_brief

# 加载虚拟环境
if [ -f ~/.hermes/hermes-agent-main/.venv/bin/activate ]; then
    source ~/.hermes/hermes-agent-main/.venv/bin/activate
fi

# 加载环境变量
if [ -f /tmp/ai_morning_brief/.env ]; then
    export $(grep -v '^#' /tmp/ai_morning_brief/.env | xargs)
fi

# 设置代理
export https_proxy=http://172.23.80.1:7890
export http_proxy=http://172.23.80.1:7890

# 轮询间隔（秒），默认 300（5 分钟）
export POLL_INTERVAL_SECONDS=${POLL_INTERVAL_SECONDS:-300}

echo "================================================"
echo "  AI 晨报 · 实时轮询系统"
echo "  代理: $https_proxy"
echo "  轮询间隔: ${POLL_INTERVAL_SECONDS}s"
echo "================================================"

# 启动轮询守护进程
python3 realtime_poller.py
