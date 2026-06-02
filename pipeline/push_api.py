#!/usr/bin/env python3
"""
Push API Blueprint — 推送订阅管理
===================================
提供设备注册/注销、VAPID 密钥获取、测试推送等接口。
"""

import os
import sys
import json
import logging
from flask import Blueprint, request, jsonify

# ── 路径 ───────────────────────────────────────────────────────────────────
WORKDIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKDIR)

from database import register_device, unregister_device, get_active_devices

logger = logging.getLogger("push_api")

# ── Blueprint ──────────────────────────────────────────────────────────────

push_bp = Blueprint("push", __name__, url_prefix="/api/v2/push")

# ── 从 push_service 获取配置 ───────────────────────────────────────────────

try:
    from push_service import (
        VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY,
        _is_quiet_hours, get_push_service,
    )
except ImportError as e:
    logger.warning(f"push_service 导入失败: {e}")
    VAPID_PUBLIC_KEY = ""
    VAPID_PRIVATE_KEY = ""
    _is_quiet_hours = lambda: False
    get_push_service = lambda: None


# ══════════════════════════════════════════════════════════════════════════════
# API 端点
# ══════════════════════════════════════════════════════════════════════════════


@push_bp.route("/subscribe", methods=["POST"])
def push_subscribe():
    """
    设备注册

    请求体:
    {
      "platform": "web" | "ios" | "android",
      "token": "device_token_or_unique_id",
      "user_id": "optional_user_id",
      "subscription": {            // Web Push only
        "endpoint": "...",
        "keys": {
          "p256dh": "...",
          "auth": "..."
        }
      }
    }

    响应:
    { "status": "ok", "message": "..." }
    """
    try:
        data = request.get_json(force=True)

        platform = data.get("platform", "web")
        token = data.get("token", "")
        user_id = data.get("user_id", "")

        # 对于 Web Push，endpoint 作为 token
        subscription = data.get("subscription", {})
        endpoint = subscription.get("endpoint", token) if subscription else token
        p256dh = subscription.get("keys", {}).get("p256dh", "") if subscription else ""
        auth = subscription.get("keys", {}).get("auth", "") if subscription else ""

        # 如果没有显式 token，使用 endpoint hash 作为唯一标识
        if not token and endpoint:
            import hashlib
            token = hashlib.sha256(endpoint.encode()).hexdigest()[:32]
        elif not token:
            token = endpoint

        if not token:
            return jsonify({"error": "token 或 subscription.endpoint 不能为空"}), 400

        # 注册到数据库
        ok = register_device(
            user_id=user_id,
            token=token,
            platform=platform,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
        )

        if ok:
            logger.info(f"✅ 设备注册成功: platform={platform}, user={user_id or 'anon'}")
            return jsonify({
                "status": "ok",
                "message": "设备已注册",
                "platform": platform,
            })
        else:
            return jsonify({"error": "注册失败"}), 500

    except Exception as e:
        logger.error(f"❌ /subscribe 错误: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@push_bp.route("/unsubscribe", methods=["POST"])
def push_unsubscribe():
    """
    设备注销

    请求体:
    {
      "token": "device_token_or_endpoint"
    }

    响应:
    { "status": "ok", "message": "..." }
    """
    try:
        data = request.get_json(force=True)
        token = data.get("token", "")

        if not token:
            return jsonify({"error": "token 不能为空"}), 400

        ok = unregister_device(token)

        if ok:
            logger.info(f"🗑️  设备注销成功: token={token[:32]}...")
            return jsonify({"status": "ok", "message": "设备已注销"})
        else:
            return jsonify({"status": "ok", "message": "设备未找到或已注销"})

    except Exception as e:
        logger.error(f"❌ /unsubscribe 错误: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@push_bp.route("/vapid-key", methods=["GET"])
def push_vapid_key():
    """
    获取 VAPID 公钥

    响应:
    {
      "publicKey": "BPd4k...",
      "available": true
    }
    """
    return jsonify({
        "publicKey": VAPID_PUBLIC_KEY,
        "available": bool(VAPID_PUBLIC_KEY),
    })


@push_bp.route("/test", methods=["POST"])
def push_test():
    """
    测试推送（仅开发环境）

    请求体:
    {
      "title": "测试标题",
      "body": "测试内容",
      "push_type": "realtime" | "breaking" | "daily_digest"
    }

    响应:
    {
      "status": "ok",
      "sent": 3,
      "platforms": {"web": 2, "ios": 1},
      "quiet_hours": false
    }
    """
    try:
        data = request.get_json(force=True)
        title = data.get("title", "AI晨报 · 测试推送")
        body = data.get("body", "这是一条测试推送消息")
        push_type = data.get("push_type", "realtime")

        svc = get_push_service()
        if svc is None:
            return jsonify({"error": "推送服务未初始化"}), 503

        # 构建测试 article
        test_article = {
            "id": f"test_{int(__import__('time').time())}",
            "title": title,
            "title_cn": title,
            "summary": body,
            "summary_cn": body,
            "ai_comment": body,
            "category": "技术突破",
            "tags": ["test"],
            "credibility": "high",
        }

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        sent = loop.run_until_complete(
            svc._send_to_all(
                article=test_article,
                push_type=push_type,
                skip_quiet_hours=True,  # 测试推送突破静音
            )
        )

        loop.close()

        # 统计各平台推送数
        devices = get_active_devices()
        platform_counts = {"web": 0, "ios": 0, "android": 0}
        for d in devices:
            pl = d.get("platform", "web")
            platform_counts[pl] = platform_counts.get(pl, 0) + 1

        return jsonify({
            "status": "ok",
            "message": f"测试推送已发送到 {sent} 个设备",
            "sent": sent,
            "total_devices": len(devices),
            "platforms": platform_counts,
            "quiet_hours": _is_quiet_hours(),
        })

    except Exception as e:
        logger.error(f"❌ /test 错误: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@push_bp.route("/status", methods=["GET"])
def push_status():
    """
    获取推送系统状态

    响应:
    {
      "web_push_available": true,
      "apns_available": false,
      "vapid_public_key": "BPd4k...",
      "quiet_hours": false,
      "active_devices": 5
    }
    """
    try:
        svc = get_push_service()
        devices = get_active_devices()

        platform_counts = {}
        for d in devices:
            pl = d.get("platform", "web")
            platform_counts[pl] = platform_counts.get(pl, 0) + 1

        return jsonify({
            "web_push_available": svc.webpush.available if svc else False,
            "apns_available": svc.apns.available if svc else False,
            "vapid_public_key": VAPID_PUBLIC_KEY[:32] + "..." if VAPID_PUBLIC_KEY else "",
            "quiet_hours": _is_quiet_hours(),
            "active_devices": len(devices),
            "platforms": platform_counts,
            "rate_limit": {
                "max_per_15min": __import__('push_service').MAX_PUSH_PER_USER_PER_15MIN,
            },
        })

    except Exception as e:
        logger.error(f"❌ /status 错误: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@push_bp.route("/devices", methods=["GET"])
def push_devices():
    """
    获取所有活跃设备列表（管理用）

    Query: platform (可选筛选)

    响应:
    {
      "devices": [
        {
          "id": 1,
          "user_id": "...",
          "platform": "web",
          "token": "...",
          "created_at": "...",
          "last_seen": "..."
        }
      ],
      "total": 5
    }
    """
    try:
        platform_filter = request.args.get("platform", "")

        devices = get_active_devices()

        if platform_filter:
            devices = [d for d in devices if d.get("platform") == platform_filter]

        # 脱敏：隐藏完整 token/endpoint
        safe_devices = []
        for d in devices:
            sd = dict(d)
            if "token" in sd and sd["token"]:
                sd["token"] = sd["token"][:16] + "..." if len(sd["token"]) > 16 else sd["token"]
            if "endpoint" in sd and sd["endpoint"]:
                sd["endpoint"] = sd["endpoint"][:50] + "..." if len(sd["endpoint"]) > 50 else sd["endpoint"]
            if "p256dh" in sd:
                sd["p256dh"] = "(hidden)"
            if "auth" in sd:
                sd["auth"] = "(hidden)"
            safe_devices.append(sd)

        return jsonify({
            "devices": safe_devices,
            "total": len(safe_devices),
        })

    except Exception as e:
        logger.error(f"❌ /devices 错误: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
