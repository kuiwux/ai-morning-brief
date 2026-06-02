# =============================================================================
# Parro — Dockerfile
# 基于 python:3.11-slim
# =============================================================================

FROM python:3.11-slim

LABEL maintainer="Parro Team"
LABEL description="Parro (硅谷AI晨报) - Flask Backend + Pipeline Poller"

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8899

# 安装系统依赖
# - yt-dlp: 用于 YouTube 数据抓取（需要 ffmpeg 处理音频）
# - supervisor: 进程管理（同时运行 Flask + Poller）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    supervisor \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 安装 yt-dlp（Python 包，而非系统包）
RUN pip install --no-cache-dir yt-dlp

# 创建工作目录
WORKDIR /app

# ── 依赖安装（利用 Docker 缓存分层）──
# 先复制 requirements 文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pipeline/requirements.txt pipeline/requirements.txt
RUN pip install --no-cache-dir -r pipeline/requirements.txt

# 安装 gunicorn（生产 WSGI 服务器）
RUN pip install --no-cache-dir gunicorn

# ── 复制应用代码 ──
COPY . .

# 创建必要目录
RUN mkdir -p /app/data /app/logs /app/run

# ── Supervisor 配置 ──
# 使用 supervisord 同时运行 Flask + Poller
COPY deploy/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 暴露端口
EXPOSE 8899

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8899/ || exit 1

# 启动 supervisord 管理所有进程
CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
