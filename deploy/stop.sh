#!/usr/bin/env bash
# =============================================================================
# Parro — 优雅停止所有服务
# 用法: ./deploy/stop.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_DIR="${PROJECT_DIR}/run"

FLASK_PID_FILE="${PID_DIR}/flask.pid"
POLLER_PID_FILE="${PID_DIR}/poller.pid"

STOP_TIMEOUT=10  # 优雅停止等待秒数

echo "🛑 正在停止 Parro 服务..."

stopped_count=0

# ── 停止 Poller（先停，因为它依赖 Flask）──
if [ -f "${POLLER_PID_FILE}" ]; then
    POLLER_PID=$(cat "${POLLER_PID_FILE}")
    if kill -0 "${POLLER_PID}" 2>/dev/null; then
        echo "   🔄 停止 Poller (PID: ${POLLER_PID})..."
        # 发送 SIGTERM（realtime_poller.py 支持优雅退出）
        kill -TERM "${POLLER_PID}" 2>/dev/null || true

        # 等待进程退出
        for i in $(seq 1 ${STOP_TIMEOUT}); do
            if ! kill -0 "${POLLER_PID}" 2>/dev/null; then
                echo "      ✅ Poller 已停止"
                stopped_count=$((stopped_count + 1))
                break
            fi
            sleep 1
        done

        # 强制杀死
        if kill -0 "${POLLER_PID}" 2>/dev/null; then
            echo "      ⚠️  Poller 未响应，强制停止..."
            kill -9 "${POLLER_PID}" 2>/dev/null || true
            echo "      ✅ Poller 已强制停止"
            stopped_count=$((stopped_count + 1))
        fi
    else
        echo "   ℹ️  Poller 已不在运行"
    fi
    rm -f "${POLLER_PID_FILE}"
else
    echo "   ℹ️  未找到 Poller PID 文件"
fi

# ── 停止 Flask ──
if [ -f "${FLASK_PID_FILE}" ]; then
    FLASK_PID=$(cat "${FLASK_PID_FILE}")
    if kill -0 "${FLASK_PID}" 2>/dev/null; then
        echo "   📡 停止 Flask (PID: ${FLASK_PID})..."
        kill -TERM "${FLASK_PID}" 2>/dev/null || true

        for i in $(seq 1 ${STOP_TIMEOUT}); do
            if ! kill -0 "${FLASK_PID}" 2>/dev/null; then
                echo "      ✅ Flask 已停止"
                stopped_count=$((stopped_count + 1))
                break
            fi
            sleep 1
        done

        if kill -0 "${FLASK_PID}" 2>/dev/null; then
            echo "      ⚠️  Flask 未响应，强制停止..."
            kill -9 "${FLASK_PID}" 2>/dev/null || true
            echo "      ✅ Flask 已强制停止"
            stopped_count=$((stopped_count + 1))
        fi
    else
        echo "   ℹ️  Flask 已不在运行"
    fi
    rm -f "${FLASK_PID_FILE}"
else
    echo "   ℹ️  未找到 Flask PID 文件"
fi

# ── 清理存活进程 ──
# 确保端口 8899 上没有残留进程
PORT="${PORT:-8899}"
if lsof -i :${PORT} -t &>/dev/null; then
    echo "   🧹 清理端口 ${PORT} 上的残留进程..."
    lsof -i :${PORT} -t | xargs kill -9 2>/dev/null || true
fi

echo ""
echo "✅ Parro 服务已全部停止"
