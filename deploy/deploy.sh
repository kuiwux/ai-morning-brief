#!/usr/bin/env bash
# =============================================================================
# Parro — 一键部署脚本
# 功能: git pull → pip install → 重启所有服务
# 用法: ./deploy/deploy.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "${PROJECT_DIR}"

echo "🦜 Parro 一键部署"
echo "=================="
echo ""

# ── 1. 拉取最新代码 ──
echo "📥 拉取最新代码..."
git pull origin master 2>/dev/null || echo "   ⚠️  git pull 跳过（可能已是最新或无法连接远程仓库）"

# ── 2. 安装/更新依赖 ──
echo ""
echo "📦 安装 Python 依赖..."
pip install -r requirements.txt -q
pip install -r pipeline/requirements.txt -q
echo "   ✅ 依赖已更新"

# ── 3. 数据库迁移（如需要）──
echo ""
echo "🗄️  检查数据库..."
python3 -c "from database import init_db; init_db(); print('   ✅ 数据库就绪')"

# ── 4. 重启服务 ──
echo ""
echo "🔄 重启服务..."
bash "${SCRIPT_DIR}/stop.sh" 2>/dev/null || true
sleep 2
bash "${SCRIPT_DIR}/start.sh"

echo ""
echo "✅ 部署完成"
