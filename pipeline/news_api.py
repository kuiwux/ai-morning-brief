"""
News API v2 — REST API 端点（挂载到 Flask server.py）

端点数：
  GET  /api/v2/articles          → 资讯列表
  GET  /api/v2/articles/<id>     → 文章详情
  GET  /api/v2/articles/search   → 全文搜索
  GET  /api/v2/history/<date>    → 按日期查询历史
  GET  /api/v2/stats             → 统计
  POST /api/v2/push/subscribe    → 设备注册推送
  POST /api/v2/push/unsubscribe  → 设备注销推送
  GET  /api/v2/vapid-public-key  → 获取 VAPID 公钥
  POST /api/v2/favorites         → 收藏 toggle
  GET  /api/v2/favorites         → 收藏列表
  POST /api/v2/preferences       → 用户偏好
  GET  /api/v2/preferences       → 获取偏好
  POST /api/v2/chat              → AI 对话
  POST /api/v2/voice/clone       → 语音克隆
  GET  /api/v2/voice/list        → 语音列表
"""

import os
import sys
import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Blueprint, request, jsonify, current_app
from openai import OpenAI

from database import (
    get_db, get_articles, get_article_by_id, search_articles,
    get_history_by_date, get_stats as db_get_stats,
    register_device, unregister_device,
)

logger = logging.getLogger("news_api")

# ── Blueprint ───────────────────────────────────────────────────────────────
news_bp = Blueprint("news_v2", __name__, url_prefix="/api/v2")

# ── 认证 ────────────────────────────────────────────────────────────────────
API_TOKEN = os.environ.get("API_TOKEN", "hermes-morning-brief-2026")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

if not DEEPSEEK_API_KEY:
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    DEEPSEEK_API_KEY = line.split("=", 1)[1]
                    break

ds_client = None
if DEEPSEEK_API_KEY:
    ds_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

WORKDIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(WORKDIR, "data.db")

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get(
    "VAPID_PUBLIC_KEY",
    "BPK5DqQFq-lXz7jV9QXIZ1qXHpYQkQjSMpXG3GXg3X_XXXXX"  # placeholder
)


def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def optional_token(f):
    """部分端点可选认证"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        request.authenticated = (token == API_TOKEN)
        return f(*args, **kwargs)
    return decorated


def article_to_dict(row) -> dict:
    """将 sqlite3.Row 转为对外 JSON 格式"""
    d = dict(row)
    if "tags" in d and isinstance(d["tags"], str):
        try:
            d["tags"] = json.loads(d["tags"])
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
    return d


# ══════════════════════════════════════════════════════════════════════════════
# 资讯 API
# ══════════════════════════════════════════════════════════════════════════════

@news_bp.route("/articles", methods=["GET"])
def articles_list():
    """
    资讯列表
    Query: ?category= & ?region= & ?since= & ?limit= & ?offset=
    """
    category = request.args.get("category", None)
    region = request.args.get("region", None)
    since = request.args.get("since", None)
    limit = request.args.get("limit", 20, type=int)
    offset = request.args.get("offset", 0, type=int)

    limit = max(1, min(limit, 100))

    articles, total = get_articles(
        category=category, region=region, since=since,
        limit=limit, offset=offset,
    )
    return jsonify({
        "articles": articles,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@news_bp.route("/articles/<article_id>", methods=["GET"])
def article_detail(article_id: str):
    """文章详情"""
    article = get_article_by_id(article_id)
    if not article:
        return jsonify({"error": "文章不存在"}), 404
    return jsonify(article)


@news_bp.route("/articles/search", methods=["GET"])
def articles_search():
    """
    全文搜索
    Query: ?q=关键词
    """
    q = request.args.get("q", "")
    if not q:
        return jsonify({"error": "q 参数不能为空"}), 400

    articles = search_articles(q, limit=50)
    return jsonify({
        "query": q,
        "articles": articles,
        "total": len(articles),
    })


@news_bp.route("/history/<date>", methods=["GET"])
def history_by_date(date: str):
    """按日期查询历史资讯"""
    articles = get_history_by_date(date)
    return jsonify({
        "date": date,
        "articles": articles,
        "total": len(articles),
    })


@news_bp.route("/stats", methods=["GET"])
def stats():
    """统计信息"""
    return jsonify(db_get_stats())


# ══════════════════════════════════════════════════════════════════════════════
# 推送订阅 API
# ══════════════════════════════════════════════════════════════════════════════

@news_bp.route("/push/subscribe", methods=["POST"])
@require_token
def push_subscribe():
    """
    设备注册推送
    Body: {user_id, token, platform?, endpoint?, p256dh?, auth?}
    """
    try:
        data = request.get_json(force=True)
        user_id = data.get("user_id", "")
        token = data.get("token", "")
        platform = data.get("platform", "web")
        endpoint = data.get("endpoint", "")
        p256dh = data.get("p256dh", "")
        auth = data.get("auth", "")

        if not user_id or not token:
            return jsonify({"error": "user_id 和 token 不能为空"}), 400

        ok = register_device(
            user_id=user_id, token=token, platform=platform,
            endpoint=endpoint, p256dh=p256dh, auth=auth,
        )
        if ok:
            return jsonify({"status": "ok", "message": "设备已注册"})
        else:
            return jsonify({"error": "注册失败"}), 500

    except Exception as e:
        logger.error(f"push/subscribe error: {e}")
        return jsonify({"error": "Internal server error"}), 500


@news_bp.route("/push/unsubscribe", methods=["POST"])
@require_token
def push_unsubscribe():
    """
    设备注销推送
    Body: {token}
    """
    try:
        data = request.get_json(force=True)
        token = data.get("token", "")

        if not token:
            return jsonify({"error": "token 不能为空"}), 400

        ok = unregister_device(token)
        return jsonify({"status": "ok", "unregistered": ok})

    except Exception as e:
        logger.error(f"push/unsubscribe error: {e}")
        return jsonify({"error": "Internal server error"}), 500


@news_bp.route("/vapid-public-key", methods=["GET"])
def vapid_public_key():
    """获取 VAPID 公钥"""
    return jsonify({"public_key": VAPID_PUBLIC_KEY})


# ══════════════════════════════════════════════════════════════════════════════
# 收藏 API
# ══════════════════════════════════════════════════════════════════════════════

@news_bp.route("/favorites", methods=["POST"])
@require_token
def favorites_toggle():
    """收藏 toggle"""
    try:
        data = request.get_json(force=True)
        user_id = data.get("user_id", "")
        article_id = data.get("article_id", "")

        if not user_id or not article_id:
            return jsonify({"error": "user_id 和 article_id 不能为空"}), 400

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND article_id=?",
            (user_id, article_id),
        )
        exists = cursor.fetchone()

        if exists:
            cursor.execute(
                "DELETE FROM favorites WHERE user_id=? AND article_id=?",
                (user_id, article_id),
            )
            action = "removed"
        else:
            cursor.execute(
                "INSERT INTO favorites (user_id, article_id) VALUES (?, ?)",
                (user_id, article_id),
            )
            action = "added"

        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "action": action})

    except Exception as e:
        logger.error(f"favorites toggle error: {e}")
        return jsonify({"error": "Internal server error"}), 500


@news_bp.route("/favorites", methods=["GET"])
@require_token
def favorites_get():
    """获取收藏列表"""
    try:
        user_id = request.args.get("user_id", "")
        if not user_id:
            return jsonify({"error": "user_id 不能为空"}), 400

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT article_id FROM favorites WHERE user_id=?",
            (user_id,),
        )
        rows = cursor.fetchall()
        conn.close()

        article_ids = [row["article_id"] for row in rows]
        return jsonify({"user_id": user_id, "favorites": article_ids, "total": len(article_ids)})

    except Exception as e:
        logger.error(f"favorites get error: {e}")
        return jsonify({"error": "Internal server error"}), 500


# ══════════════════════════════════════════════════════════════════════════════
# 用户偏好 API
# ══════════════════════════════════════════════════════════════════════════════

@news_bp.route("/preferences", methods=["POST"])
@require_token
def preferences_save():
    """保存用户偏好"""
    try:
        data = request.get_json(force=True)
        user_id = data.get("user_id", "")
        category = data.get("category", "")
        preference = data.get("preference", "")

        if not user_id or not category or not preference:
            return jsonify({"error": "user_id, category, preference 不能为空"}), 400

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO user_prefs (user_id, category, preference) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, category) DO UPDATE SET preference=excluded.preference",
            (user_id, category, preference),
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})

    except Exception as e:
        logger.error(f"preferences save error: {e}")
        return jsonify({"error": "Internal server error"}), 500


@news_bp.route("/preferences", methods=["GET"])
@require_token
def preferences_get():
    """获取用户偏好"""
    try:
        user_id = request.args.get("user_id", "")
        if not user_id:
            return jsonify({"error": "user_id 不能为空"}), 400

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT category, preference FROM user_prefs WHERE user_id=?",
            (user_id,),
        )
        rows = cursor.fetchall()
        conn.close()

        prefs = {row["category"]: row["preference"] for row in rows}
        return jsonify({"user_id": user_id, "preferences": prefs})

    except Exception as e:
        logger.error(f"preferences get error: {e}")
        return jsonify({"error": "Internal server error"}), 500


# ── 语音目录 ──
VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices")
os.makedirs(VOICE_DIR, exist_ok=True)


@news_bp.route("/voice/clone", methods=["POST"])
@require_token
def voice_clone():
    """
    语音克隆 — 文件上传
    Body: multipart/form-data, 字段 name, file
    """
    try:
        name = request.form.get("name", "")
        if "file" not in request.files:
            return jsonify({"error": "请上传音频文件"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "文件名为空"}), 400

        # 保存文件
        safe_name = name or os.path.splitext(file.filename)[0]
        ext = os.path.splitext(file.filename)[1] or ".wav"
        filepath = os.path.join(VOICE_DIR, f"{safe_name}{ext}")
        file.save(filepath)

        return jsonify({
            "status": "ok",
            "message": f"语音样本已保存: {safe_name}",
            "path": filepath,
        })

    except Exception as e:
        logger.error(f"voice clone error: {e}")
        return jsonify({"error": "Internal server error"}), 500


@news_bp.route("/voice/list", methods=["GET"])
def voice_list():
    """获取已克隆的语音列表"""
    try:
        voices = []
        if os.path.exists(VOICE_DIR):
            for fname in os.listdir(VOICE_DIR):
                if fname.endswith((".wav", ".mp3", ".ogg", ".flac")):
                    voices.append({
                        "name": os.path.splitext(fname)[0],
                        "file": fname,
                        "path": os.path.join(VOICE_DIR, fname),
                    })
        return jsonify({"voices": voices, "total": len(voices)})

    except Exception as e:
        logger.error(f"voice list error: {e}")
        return jsonify({"error": "Internal server error"}), 500
