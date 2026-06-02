#!/usr/bin/env python3
"""
认证 API — Flask Blueprint（挂载到 server.py）

端点：
  POST /api/v2/auth/send-code     → 发送验证码
  POST /api/v2/auth/login-phone   → 手机号登录
  POST /api/v2/auth/login-guest   → 游客登录
  POST /api/v2/auth/login-wechat  → 微信登录
  POST /api/v2/auth/refresh       → 刷新 token
  GET  /api/v2/auth/user          → 获取当前用户信息
  PUT  /api/v2/auth/user          → 更新用户资料
  POST /api/v2/auth/logout        → 登出
  POST /api/v2/auth/bind-phone    → 游客绑定手机号
"""

import logging
from functools import wraps

from flask import Blueprint, request, jsonify, g

from auth_service import (
    send_sms_code,
    login_phone,
    login_guest,
    login_wechat,
    verify_jwt,
    refresh_jwt,
    get_current_user,
    update_profile,
)
from user_db import bind_phone
from user_db import init_auth_db

logger = logging.getLogger("auth_api")

# ── Blueprint ───────────────────────────────────────────────────────────────
auth_bp = Blueprint("auth", __name__, url_prefix="/api/v2/auth")


# ── require_auth 装饰器 ────────────────────────────────────────────────────

def require_auth(f):
    """
    JWT 认证装饰器
    从 Authorization header 提取 Bearer token，验证后注入 g.user_id
    游客模式也可通过，但 g 中会标记 g.is_guest
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()

        if not token:
            return jsonify({"error": "未提供认证令牌", "code": "NO_TOKEN"}), 401

        user_id = verify_jwt(token)
        if not user_id:
            return jsonify({"error": "令牌无效或已过期", "code": "INVALID_TOKEN"}), 401

        g.user_id = user_id
        g.token = token

        # 获取用户信息以判断是否为游客
        user = get_current_user(user_id)
        if user:
            g.is_guest = user.get("is_guest", False)
            g.member_type = user.get("member_type", "free")

        return f(*args, **kwargs)

    return decorated


def require_member(f):
    """
    会员认证装饰器 — 非游客可访问
    """
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if getattr(g, "is_guest", True):
            return jsonify({
                "error": "游客无法进行此操作，请先登录",
                "code": "GUEST_RESTRICTED",
            }), 403
        return f(*args, **kwargs)

    return decorated


# ── API 端点 ────────────────────────────────────────────────────────────────

@auth_bp.route("/send-code", methods=["POST"])
def api_send_code():
    """
    发送短信验证码
    POST /api/v2/auth/send-code
    Body: { "phone": "13800138000" }
    """
    try:
        data = request.get_json(force=True)
        phone = (data.get("phone") or "").strip()

        if not phone:
            return jsonify({"error": "手机号不能为空"}), 400

        # 简单校验中国大陆手机号
        import re
        if not re.match(r"^1[3-9]\d{9}$", phone):
            return jsonify({"error": "手机号格式不正确"}), 400

        result = send_sms_code(phone)
        return jsonify({"status": "ok", **result})

    except Exception as e:
        logger.error(f"send-code 错误: {e}", exc_info=True)
        return jsonify({"error": "发送验证码失败"}), 500


@auth_bp.route("/login-phone", methods=["POST"])
def api_login_phone():
    """
    手机号验证码登录
    POST /api/v2/auth/login-phone
    Body: { "phone": "13800138000", "code": "123456" }
    """
    try:
        data = request.get_json(force=True)
        phone = (data.get("phone") or "").strip()
        code = (data.get("code") or "").strip()

        if not phone:
            return jsonify({"error": "手机号不能为空"}), 400
        if not code:
            return jsonify({"error": "验证码不能为空"}), 400

        token, user, error = login_phone(phone, code)

        if error:
            return jsonify({"error": error}), 401

        return jsonify({
            "status": "ok",
            "token": token,
            "user": user,
            "expires_in": 30 * 86400,
        })

    except Exception as e:
        logger.error(f"login-phone 错误: {e}", exc_info=True)
        return jsonify({"error": "登录失败"}), 500


@auth_bp.route("/login-guest", methods=["POST"])
def api_login_guest():
    """
    游客登录（一键进入，无需任何输入）
    POST /api/v2/auth/login-guest
    Body: {}（可选）
    """
    try:
        token, user = login_guest()
        return jsonify({
            "status": "ok",
            "token": token,
            "user": user,
            "expires_in": 30 * 86400,
            "tips": "游客模式：每天可追问 AI 10 次，7天未登录将自动清理",
        })

    except Exception as e:
        logger.error(f"login-guest 错误: {e}", exc_info=True)
        return jsonify({"error": "游客登录失败"}), 500


@auth_bp.route("/login-wechat", methods=["POST"])
def api_login_wechat():
    """
    微信 OAuth 登录
    POST /api/v2/auth/login-wechat
    Body: { "code": "微信授权code" }
    """
    try:
        data = request.get_json(force=True)
        code = (data.get("code") or "").strip()

        if not code:
            return jsonify({"error": "微信授权 code 不能为空"}), 400

        token, user, error = login_wechat(code)

        if error:
            return jsonify({"error": error}), 401

        return jsonify({
            "status": "ok",
            "token": token,
            "user": user,
            "expires_in": 30 * 86400,
        })

    except Exception as e:
        logger.error(f"login-wechat 错误: {e}", exc_info=True)
        return jsonify({"error": "微信登录失败"}), 500


@auth_bp.route("/refresh", methods=["POST"])
def api_refresh_token():
    """
    刷新 JWT Token
    POST /api/v2/auth/refresh
    Headers: Authorization: Bearer <old_token>
    """
    try:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()

        if not token:
            return jsonify({"error": "未提供令牌"}), 400

        new_token = refresh_jwt(token)
        if not new_token:
            return jsonify({"error": "令牌无效或已过期"}), 401

        return jsonify({
            "status": "ok",
            "token": new_token,
            "expires_in": 30 * 86400,
        })

    except Exception as e:
        logger.error(f"refresh 错误: {e}", exc_info=True)
        return jsonify({"error": "刷新令牌失败"}), 500


@auth_bp.route("/user", methods=["GET"])
@require_auth
def api_get_user():
    """
    获取当前用户信息
    GET /api/v2/auth/user
    Headers: Authorization: Bearer <token>
    """
    try:
        user = get_current_user(g.user_id)
        if not user:
            return jsonify({"error": "用户不存在"}), 404

        return jsonify({"status": "ok", "user": user})

    except Exception as e:
        logger.error(f"get-user 错误: {e}", exc_info=True)
        return jsonify({"error": "获取用户信息失败"}), 500


@auth_bp.route("/user", methods=["PUT"])
@require_auth
def api_update_user():
    """
    更新用户资料
    PUT /api/v2/auth/user
    Headers: Authorization: Bearer <token>
    Body: { "nickname": "新昵称", "avatar_url": "https://..." }
    """
    try:
        data = request.get_json(force=True)
        nickname = data.get("nickname")
        avatar_url = data.get("avatar_url")

        if not nickname and not avatar_url:
            return jsonify({"error": "至少提供 nickname 或 avatar_url"}), 400

        user = update_profile(g.user_id, nickname=nickname, avatar_url=avatar_url)
        if not user:
            return jsonify({"error": "用户不存在"}), 404

        return jsonify({"status": "ok", "user": user})

    except Exception as e:
        logger.error(f"update-user 错误: {e}", exc_info=True)
        return jsonify({"error": "更新用户信息失败"}), 500


@auth_bp.route("/logout", methods=["POST"])
@require_auth
def api_logout():
    """
    登出（客户端删除 token 即可，服务端无状态）
    POST /api/v2/auth/logout
    Headers: Authorization: Bearer <token>
    """
    # JWT 无状态，登出只需客户端清除 token
    # 可扩展：将 token 加入黑名单
    return jsonify({
        "status": "ok",
        "message": "已登出，请客户端清除 token",
    })


@auth_bp.route("/bind-phone", methods=["POST"])
@require_auth
def api_bind_phone():
    """
    游客绑定手机号（升级为正式用户）
    POST /api/v2/auth/bind-phone
    Headers: Authorization: Bearer <token>
    Body: { "phone": "13800138000", "code": "123456" }
    """
    try:
        data = request.get_json(force=True)
        phone = (data.get("phone") or "").strip()
        code = (data.get("code") or "").strip()

        if not phone or not code:
            return jsonify({"error": "手机号和验证码不能为空"}), 400

        # 验证码校验
        from auth_service import verify_jwt
        from user_db import verify_sms_code as db_verify

        if not db_verify(phone, code):
            return jsonify({"error": "验证码错误或已过期"}), 401

        if not bind_phone(g.user_id, phone):
            return jsonify({"error": "绑定失败，手机号可能已被其他用户使用"}), 409

        user = get_current_user(g.user_id)
        return jsonify({
            "status": "ok",
            "message": "手机号绑定成功，已升级为正式用户",
            "user": user,
        })

    except Exception as e:
        logger.error(f"bind-phone 错误: {e}", exc_info=True)
        return jsonify({"error": "绑定手机号失败"}), 500
