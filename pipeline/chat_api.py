#!/usr/bin/env python3
"""
Chat API v2 — AI 对话 REST API（挂载到 server.py）

端点：
  POST   /api/v2/chat           → 发送消息
  GET    /api/v2/chat/history   → 对话历史
  DELETE /api/v2/chat/history   → 清除对话历史
"""
import sys
import os
import asyncio
import logging

from flask import Blueprint, request, jsonify, g
from functools import wraps

# 确保可以导入同级模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chat_service import get_chat_service
from chat_db import init_chat_db

# 尝试导入 require_auth（来自 auth_api）
try:
    from auth_api import require_auth
except ImportError:
    # 降级：如果没有 auth_api，使用简单的 token 验证
    API_TOKEN = os.environ.get("API_TOKEN", "hermes-morning-brief-2026")

    def require_auth(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
            if not token:
                return jsonify({"error": "未提供认证令牌", "code": "NO_TOKEN"}), 401
            # 简单 token 模式：直接以 token 为 user_id
            g.user_id = token if token != API_TOKEN else "anonymous"
            g.member_type = "free"
            return f(*args, **kwargs)
        return decorated


logger = logging.getLogger("chat_api")

# ── Blueprint ───────────────────────────────────────────────────────────────
chat_bp = Blueprint("chat_v2", __name__, url_prefix="/api/v2")


# ── 初始化数据库 ────────────────────────────────────────────────────────────
def _ensure_db():
    """确保 chat 相关表已创建"""
    try:
        init_chat_db()
    except Exception as e:
        logger.warning(f"初始化 chat 数据库失败（可能已存在）: {e}")


# ── run_async helper ──────────────────────────────────────────────────────

def run_async(coro):
    """在同步 Flask 视图函数中运行异步协程"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 如果事件循环已在运行，创建新的
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result(timeout=35)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ── API 端点 ────────────────────────────────────────────────────────────────

@chat_bp.route("/chat", methods=["POST"])
@require_auth
def api_chat():
    """
    发送 AI 对话消息

    POST /api/v2/chat
    Headers: Authorization: Bearer <token>
    Body: {
        "question": "这个公司的估值合理吗？",
        "article_id": "hn_12345",     // 可选，基于文章追问时传入
        "mode": "article"              // 可选: article / daily_summary / trend
    }

    Response (成功):
    {
        "reply": "AI 回复内容...",
        "tokens_used": 1234,
        "remaining_queries": 9,
        "mode": "article"
    }

    Response (配额用尽):
    {
        "reply": null,
        "error": "今日AI追问次数已用完，升级Pro享无限对话",
        "remaining_queries": 0
    }
    """
    try:
        data = request.get_json(force=True)
        question = (data.get("question") or "").strip()
        article_id = data.get("article_id")
        mode = data.get("mode", "article")

        # 参数校验
        if not question:
            return jsonify({"error": "question 不能为空"}), 400

        valid_modes = ("article", "daily_summary", "trend")
        if mode not in valid_modes:
            return jsonify({
                "error": f"mode 无效，可选: {', '.join(valid_modes)}"
            }), 400

        # 模式约束：article 模式需要 article_id
        if mode == "article" and not article_id:
            return jsonify({
                "error": "article 模式需要提供 article_id"
            }), 400

        user_id = g.user_id
        service = get_chat_service()

        # 执行对话
        result = run_async(service.chat(
            user_id=user_id,
            question=question,
            article_id=article_id,
            mode=mode,
        ))

        # 根据结果返回状态码
        if result.get("error"):
            return jsonify(result), 429  # Too Many Requests

        return jsonify(result)

    except Exception as e:
        logger.error(f"/api/v2/chat 错误: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@chat_bp.route("/chat/history", methods=["GET"])
@require_auth
def api_chat_history():
    """
    获取对话历史

    GET /api/v2/chat/history?article_id=hn_12345&limit=20
    Headers: Authorization: Bearer <token>

    Response:
    {
        "history": [
            {
                "id": 1,
                "question": "...",
                "answer": "...",
                "mode": "article",
                "tokens_used": 123,
                "created_at": "2026-05-31T10:00:00"
            }
        ],
        "total": 20
    }
    """
    try:
        user_id = g.user_id
        article_id = request.args.get("article_id")
        limit = request.args.get("limit", 20, type=int)
        limit = min(max(1, limit), 100)  # 限制 1-100

        service = get_chat_service()
        history = run_async(service.get_history(user_id, article_id, limit))

        # 精简返回字段
        items = [{
            "id": h["id"],
            "question": h["question"],
            "answer": h["answer"],
            "mode": h.get("mode", "article"),
            "article_id": h.get("article_id"),
            "tokens_used": h.get("tokens_used", 0),
            "created_at": h["created_at"],
        } for h in history]

        return jsonify({
            "history": items,
            "total": len(items),
        })

    except Exception as e:
        logger.error(f"/api/v2/chat/history 错误: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@chat_bp.route("/chat/history", methods=["DELETE"])
@require_auth
def api_chat_clear_history():
    """
    清除对话历史

    DELETE /api/v2/chat/history?article_id=hn_12345
    Headers: Authorization: Bearer <token>

    不传 article_id 则清除该用户所有对话历史

    Response:
    {
        "status": "ok",
        "deleted": 5
    }
    """
    try:
        user_id = g.user_id
        article_id = request.args.get("article_id")

        service = get_chat_service()
        deleted = run_async(service.clear_history(user_id, article_id))

        return jsonify({
            "status": "ok",
            "deleted": deleted,
        })

    except Exception as e:
        logger.error(f"/api/v2/chat/history DELETE 错误: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# ── 初始化 ──────────────────────────────────────────────────────────────────
_ensure_db()
