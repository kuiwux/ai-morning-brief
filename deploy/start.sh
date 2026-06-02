#!/usr/bin/env bash
# =============================================================================
# Parro — 生产环境启动脚本
# 功能: 加载环境变量，启动 Flask 后端 + Pipeline 轮询器
# 用法: ./deploy/start.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="${PROJECT_DIR}/logs"
PID_DIR="${PROJECT_DIR}/run"

# PID 文件路径
FLASK_PID_FILE="${PID_DIR}/flask.pid"
POLLER_PID_FILE="${PID_DIR}/poller.pid"

# 日志文件路径
FLASK_LOG="${LOG_DIR}/flask.log"
POLLER_LOG="${LOG_DIR}/poller.log"

# =============================================================================
# 准备工作
# =============================================================================

echo "🦜 Parro 启动脚本"
echo "=================="

# 创建必要目录
mkdir -p "${LOG_DIR}" "${PID_DIR}"

# 加载 .env 文件
if [ -f "${PROJECT_DIR}/.env" ]; then
    echo "📄 加载 .env 环境变量..."
    set -a
    source "${PROJECT_DIR}/.env"
    set +a
else
    echo "⚠️  未找到 .env 文件，使用默认环境变量"
    echo "   请从 .env.example 复制并填写真实值"
fi

# 切换到项目目录
cd "${PROJECT_DIR}"

# =============================================================================
# Python 依赖检查
# =============================================================================

if ! python3 -c "import flask" 2>/dev/null; then
    echo "❌ 缺少 Python 依赖，请先安装:"
    echo "   pip install -r requirements.txt"
    echo "   pip install -r pipeline/requirements.txt"
    exit 1
fi

# =============================================================================
# 端口检查
# =============================================================================

PORT="${PORT:-8899}"
if lsof -i :${PORT} -t &>/dev/null; then
    echo "⚠️  端口 ${PORT} 已被占用"
    echo "   正在运行的进程:"
    lsof -i :${PORT} 2>/dev/null || true
    read -rp "   是否继续启动？(可能产生冲突) [y/N]: " answer
    if [[ ! "$answer" =~ ^[Yy] ]]; then
        exit 1
    fi
fi

# =============================================================================
# 停止已有进程
# =============================================================================

if [ -f "${FLASK_PID_FILE}" ]; then
    OLD_PID=$(cat "${FLASK_PID_FILE}")
    if kill -0 "${OLD_PID}" 2>/dev/null; then
        echo "⚠️  Flask 已在运行 (PID: ${OLD_PID})，正在停止..."
        kill "${OLD_PID}" 2>/dev/null || true
        sleep 2
        kill -9 "${OLD_PID}" 2>/dev/null || true
    fi
    rm -f "${FLASK_PID_FILE}"
fi

if [ -f "${POLLER_PID_FILE}" ]; then
    OLD_PID=$(cat "${POLLER_PID_FILE}")
    if kill -0 "${OLD_PID}" 2>/dev/null; then
        echo "⚠️  Poller 已在运行 (PID: ${OLD_PID})，正在停止..."
        kill "${OLD_PID}" 2>/dev/null || true
        sleep 2
        kill -9 "${OLD_PID}" 2>/dev/null || true
    fi
    rm -f "${POLLER_PID_FILE}"
fi

# =============================================================================
# 启动 Flask 后端
# =============================================================================

echo ""
echo "🚀 启动 Flask 后端..."

# 优先使用 gunicorn 生产模式，fallback 到 python server.py
if command -v gunicorn &>/dev/null; then
    echo "   模式: gunicorn (生产)"
    nohup gunicorn server:app \
        --bind "0.0.0.0:${PORT}" \
        --workers "${GUNICORN_WORKERS:-4}" \
        --worker-class sync \
        --timeout 120 \
        --access-logfile "${FLASK_LOG}" \
        --error-logfile "${FLASK_LOG}" \
        --pid "${FLASK_PID_FILE}" \
        --daemon \
        > /dev/null 2>&1
else
    echo "   模式: python server.py (开发)"
    nohup python3 server.py \
        > "${FLASK_LOG}" 2>&1 &
    echo $! > "${FLASK_PID_FILE}"
fi

sleep 2

# 验证 Flask 启动
FLASK_PID=$(cat "${FLASK_PID_FILE}")
if kill -0 "${FLASK_PID}" 2>/dev/null; then
    echo "   ✅ Flask 已启动 (PID: ${FLASK_PID})"
else
    echo "   ❌ Flask 启动失败，请检查日志: ${FLASK_LOG}"
    exit 1
fi

# =============================================================================
# 启动 Pipeline 轮询器
# =============================================================================

echo "🔄 启动 Pipeline 轮询器..."

# 使用 pipeline 目录下的 .env
cd "${PROJECT_DIR}/pipeline"
nohup python3 realtime_poller.py \
    > "${POLLER_LOG}" 2>&1 &
echo $! > "${POLLER_PID_FILE}"
cd "${PROJECT_DIR}"

sleep 2

POLLER_PID=$(cat "${POLLER_PID_FILE}")
if kill -0 "${POLLER_PID}" 2>/dev/null; then
    echo "   ✅ Poller 已启动 (PID: ${POLLER_PID})"
else
    echo "   ❌ Poller 启动失败，请检查日志: ${POLLER_LOG}"
fi

# =============================================================================
# 汇总
# =============================================================================

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  🦜 Parro 服务已启动"
echo "═══════════════════════════════════════════════════════════"
echo "  📡 Flask:     http://0.0.0.0:${PORT}"
echo "  📋 Flask PID:  ${FLASK_PID}"
echo "  🔄 Poller PID: ${POLLER_PID}"
echo "  📄 日志:       ${LOG_DIR}/"
echo "  🛑 停止:       ./deploy/stop.sh"
echo "═══════════════════════════════════════════════════════════"
