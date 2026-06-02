#!/usr/bin/env python3
"""
用户认证服务
支持：手机号验证码登录、微信OAuth、游客模式
JWT Token 管理
"""

import os
import time
import logging
from typing import Optional, Tuple

import jwt
import requests

from user_db import (
    get_user_by_id,
    get_user_by_phone,
    get_user_by_wechat_openid,
    create_user,
    update_last_login,
    save_sms_code,
    verify_sms_code,
    cleanup_expired_sms,
    cleanup_guest_users,
)
from payment_db import create_trial_subscription, get_active_subscription

logger = logging.getLogger("auth_service")

# ── 配置 ───────────────────────────────────────────────────────────────────
WORKDIR = os.path.dirname(os.path.abspath(__file__))
JWT_SECRET = os.environ.get("JWT_SECRET", "aimorning-secret-dev")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_DAYS = 30
GUEST_CLEANUP_DAYS = 7

# 微信配置（从 .env 读取）
WEIXIN_APPID = os.environ.get("WEIXIN_APPID", "")
WEIXIN_APPSECRET = os.environ.get("WEIXIN_APPSECRET", "")

# 尝试从 .env 文件加载
_env_path = os.path.join(WORKDIR, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("WEIXIN_APPID=") and not WEIXIN_APPID:
                WEIXIN_APPID = line.split("=", 1)[1].strip()
            elif line.startswith("WEIXIN_APPSECRET=") and not WEIXIN_APPSECRET:
                WEIXIN_APPSECRET = line.split("=", 1)[1].strip()


# ── JWT 工具 ───────────────────────────────────────────────────────────────

def create_jwt(user_id: str) -> str:
    """创建 JWT token"""
    now = int(time.time())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + JWT_EXPIRATION_DAYS * 86400,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token


def verify_jwt(token: str) -> Optional[str]:
    """验证 JWT，返回 user_id；失败返回 None"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        logger.warning("JWT 已过期")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"JWT 无效: {e}")
        return None


def refresh_jwt(token: str) -> Optional[str]:
    """刷新 JWT（如果原 token 有效，生成新 token）"""
    user_id = verify_jwt(token)
    if user_id:
        return create_jwt(user_id)
    return None


# ── 认证业务逻辑 ──────────────────────────────────────────────────────────

def send_sms_code(phone: str) -> dict:
    """
    发送6位验证码到手机号
    演示模式：直接返回验证码，不真正发送短信
    """
    import random
    code = f"{random.randint(0, 999999):06d}"
    save_sms_code(phone, code)

    logger.info(f"[演示模式] 验证码 {code} 已发送到 {phone}")
    return {
        "phone": phone,
        "code": code,  # 演示模式返回验证码
        "expires_in": 300,
        "message": f"验证码 {code} 已发送到 {phone}（演示模式）",
    }


def login_phone(phone: str, code: str) -> Tuple[Optional[str], Optional[dict], Optional[str]]:
    """
    手机号验证码登录
    Returns: (token, user_dict, error_message)
    """
    # 验证码校验
    if not verify_sms_code(phone, code):
        return None, None, "验证码错误或已过期"

    # 查找或创建用户
    user = get_user_by_phone(phone)
    is_new = False
    if not user:
        user = create_user(phone=phone, nickname=f"用户{phone[-4:]}")
        is_new = True
        logger.info(f"📱 新用户注册: phone={phone}, id={user['id']}")
    else:
        logger.info(f"📱 用户登录: phone={phone}, id={user['id']}")

    # 更新最后登录
    update_last_login(user["id"])

    # 新用户自动获得 3 天免费 Pro 试用
    if is_new:
        create_trial_subscription(user["id"])

    # 生成 JWT
    token = create_jwt(user["id"])

    return token, _sanitize_user(user), None


def login_guest() -> Tuple[str, dict]:
    """
    游客登录，创建临时账户
    Returns: (token, user_dict)
    """
    import uuid

    guest_nick = f"游客{uuid.uuid4().hex[:6]}"
    user = create_user(
        nickname=guest_nick,
        is_guest=True,
    )
    logger.info(f"👻 游客登录: id={user['id']}, nick={guest_nick}")

    update_last_login(user["id"])

    # 新游客用户自动获得 3 天免费 Pro 试用
    create_trial_subscription(user["id"])

    token = create_jwt(user["id"])

    return token, _sanitize_user(user)


def login_wechat(code: str) -> Tuple[Optional[str], Optional[dict], Optional[str]]:
    """
    微信 OAuth 登录
    1. 用 code 换 access_token
    2. 用 access_token 获取用户信息
    3. 查找或创建用户
    Returns: (token, user_dict, error_message)
    """
    if not WEIXIN_APPID or not WEIXIN_APPSECRET:
        return None, None, "微信登录未配置（缺少 WEIXIN_APPID / WEIXIN_APPSECRET）"

    # Step 1: code → access_token + openid
    token_url = "https://api.weixin.qq.com/sns/oauth2/access_token"
    params = {
        "appid": WEIXIN_APPID,
        "secret": WEIXIN_APPSECRET,
        "code": code,
        "grant_type": "authorization_code",
    }

    try:
        resp = requests.get(token_url, params=params, timeout=10)
        data = resp.json()
    except Exception as e:
        logger.error(f"微信 access_token 请求失败: {e}")
        return None, None, f"微信服务器请求失败: {e}"

    if "errcode" in data and data["errcode"] != 0:
        err = data.get("errmsg", "未知错误")
        logger.warning(f"微信 code 换 token 失败: {err}")
        return None, None, f"微信登录失败: {err}"

    openid = data.get("openid")
    access_token = data.get("access_token")

    if not openid:
        return None, None, "微信返回数据异常"

    # Step 2: 获取用户信息
    nickname = "AI探索者"
    avatar_url = None

    try:
        userinfo_url = "https://api.weixin.qq.com/sns/userinfo"
        ui_params = {
            "access_token": access_token,
            "openid": openid,
            "lang": "zh_CN",
        }
        ui_resp = requests.get(userinfo_url, params=ui_params, timeout=10)
        ui_data = ui_resp.json()
        if "errcode" not in ui_data:
            nickname = ui_data.get("nickname", nickname)
            avatar_url = ui_data.get("headimgurl")
    except Exception as e:
        logger.warning(f"获取微信用户信息失败: {e}，使用默认昵称")

    # Step 3: 查找或创建用户
    user = get_user_by_wechat_openid(openid)
    is_new = False
    if not user:
        user = create_user(
            wechat_openid=openid,
            nickname=nickname,
            avatar_url=avatar_url,
        )
        is_new = True
        logger.info(f"💚 微信新用户注册: openid={openid[:8]}..., nick={nickname}")
    else:
        logger.info(f"💚 微信用户登录: openid={openid[:8]}..., id={user['id']}")

    update_last_login(user["id"])

    # 新用户自动获得 3 天免费 Pro 试用
    if is_new:
        create_trial_subscription(user["id"])

    token = create_jwt(user["id"])

    return token, _sanitize_user(user), None


def get_current_user(user_id: str) -> Optional[dict]:
    """获取当前用户完整信息"""
    user = get_user_by_id(user_id)
    if user:
        return _sanitize_user(user)
    return None


def update_profile(user_id: str, nickname: str = None, avatar_url: str = None) -> Optional[dict]:
    """更新用户资料"""
    from user_db import update_user
    user = update_user(user_id, nickname=nickname, avatar_url=avatar_url)
    return _sanitize_user(user) if user else None


# ── 维护 ───────────────────────────────────────────────────────────────────

def maintenance_cleanup():
    """定期维护清理"""
    expired_count = cleanup_expired_sms()
    guest_count = cleanup_guest_users(days=GUEST_CLEANUP_DAYS)
    return {
        "expired_sms_cleaned": expired_count,
        "guest_users_cleaned": guest_count,
    }


# ── 工具 ───────────────────────────────────────────────────────────────────

def _sanitize_user(user: dict) -> dict:
    """过滤敏感字段，返回前端安全的用户信息"""
    trial_end = None
    if user:
        sub = get_active_subscription(user["id"])
        if sub and sub.get("status") == "trial":
            trial_end = sub.get("expires_at")

    return {
        "id": user["id"],
        "phone": user.get("phone", ""),
        "wechat_openid": user.get("wechat_openid", ""),
        "nickname": user.get("nickname", "AI探索者"),
        "avatar_url": user.get("avatar_url", ""),
        "member_type": user.get("member_type", "free"),
        "is_guest": user.get("is_guest", False),
        "trial_end": trial_end,
        "created_at": user.get("created_at", ""),
        "last_login": user.get("last_login", ""),
    }
