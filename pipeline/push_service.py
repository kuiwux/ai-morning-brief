#!/usr/bin/env python3
"""
推送核心引擎 — Web Push (VAPID) + APNs 双通道
================================================
支持:
  - Web Push (PWA): 安卓 Chrome/Edge, 桌面 Chrome/Edge/Firefox
  - APNs: iOS App + iOS PWA (Safari 16.4+)
  - 实时推送 / 突发新闻 / 每日精选 三种消息类型
  - 消费 notification_queue 表
  - 频率限制 + 静音时段
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional

# ── 路径 ───────────────────────────────────────────────────────────────────
WORKDIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKDIR)

from database import (
    get_db, get_pending_notifications, mark_notification_sent,
    mark_notification_failed, get_active_devices, unregister_device,
)

logger = logging.getLogger("push_service")

# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

# ── VAPID 配置（Web Push）───────────────────────────────────────────────────
VAPID_CLAIMS = {"sub": "mailto:admin@aimorning.brief"}

# 从环境变量读取或自动生成
_vapid_private = os.environ.get("VAPID_PRIVATE_KEY", "")
_vapid_public = os.environ.get("VAPID_PUBLIC_KEY", "")

if not _vapid_private or not _vapid_public:
    # 自动生成 VAPID 密钥对
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        from base64 import urlsafe_b64encode

        _key = ec.generate_private_key(ec.SECP256R1())
        _priv_raw = _key.private_numbers().private_value.to_bytes(32, 'big')
        _pub_raw = _key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )

        # URL-safe base64 without padding
        VAPID_PRIVATE_KEY = urlsafe_b64encode(_priv_raw).rstrip(b'=').decode()
        VAPID_PUBLIC_KEY = urlsafe_b64encode(_pub_raw).rstrip(b'=').decode()

        logger.info(f"🔑 自动生成 VAPID 密钥对 (public: {VAPID_PUBLIC_KEY[:16]}...)")
        logger.info("   ⚠️  生产环境请通过环境变量 VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY 配置固定密钥")
    except Exception as e:
        logger.warning(f"VAPID 密钥生成失败: {e}，Web Push 将不可用")
        VAPID_PRIVATE_KEY = ""
        VAPID_PUBLIC_KEY = ""
else:
    VAPID_PRIVATE_KEY = _vapid_private
    VAPID_PUBLIC_KEY = _vapid_public

# ── APNs 配置 ───────────────────────────────────────────────────────────────
APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "")
APNS_AUTH_KEY_PATH = os.environ.get("APNS_AUTH_KEY_PATH", "")
APNS_BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "com.aimorning.brief")
APNS_USE_SANDBOX = os.environ.get("APNS_USE_SANDBOX", "false").lower() == "true"

# ── 推送频率限制 ────────────────────────────────────────────────────────────
MAX_PUSH_PER_USER_PER_15MIN = 3       # 每用户每15分钟最多推送数量
QUIET_HOURS_START = 22                # 静音开始时间（22:00）
QUIET_HOURS_END = 7                   # 静音结束时间（07:00）
# 在静音时段内，只有 breaking 类型可以推送

# ── 推送历史（内存缓存，用于频率限制）──────────────────────────────────────
_push_history: dict[str, list[float]] = {}  # user_id → [timestamp, ...]


def _is_quiet_hours() -> bool:
    """检查当前是否在静音时段"""
    now = datetime.now(timezone.utc)
    hour = now.hour
    # 北京时间 = UTC + 8
    bj_hour = (hour + 8) % 24
    if QUIET_HOURS_START >= QUIET_HOURS_END:
        return bj_hour >= QUIET_HOURS_START or bj_hour < QUIET_HOURS_END
    else:
        return QUIET_HOURS_START <= bj_hour < QUIET_HOURS_END


def _check_rate_limit(user_id: str) -> bool:
    """检查用户是否超出频率限制，返回 True 表示允许推送"""
    now = time.time()
    window_15min = 15 * 60

    if user_id not in _push_history:
        _push_history[user_id] = []

    # 清理过期记录
    _push_history[user_id] = [
        ts for ts in _push_history[user_id]
        if now - ts < window_15min
    ]

    return len(_push_history[user_id]) < MAX_PUSH_PER_USER_PER_15MIN


def _record_push(user_id: str) -> None:
    """记录一次推送"""
    if user_id not in _push_history:
        _push_history[user_id] = []
    _push_history[user_id].append(time.time())


# ══════════════════════════════════════════════════════════════════════════════
# Payload 构建
# ══════════════════════════════════════════════════════════════════════════════

def _build_web_push_payload(article: dict, push_type: str = "realtime") -> dict:
    """构建 Web Push (PWA) 的 payload"""
    title = article.get("title_cn") or article.get("title", "AI晨报")
    body = ""

    if push_type == "breaking":
        body = f"🔴 突发 · {article.get('ai_comment', '') or article.get('summary_cn', '') or article.get('summary', '')}"
    elif push_type == "daily_digest":
        body = article.get("summary", "")
    else:
        body = (
            article.get("ai_comment", "") or
            article.get("summary_cn", "") or
            article.get("summary", "")
        )

    if body:
        # 截断 body 为合理长度
        if len(body) > 200:
            body = body[:197] + "..."

    article_id = article.get("id", "")

    return {
        "title": title[:80],
        "body": body[:200],
        "icon": "/icons/icon-192.png",
        "badge": "/icons/favicon-32.png",
        "data": {
            "articleId": article_id,
            "url": f"/article/{article_id}" if article_id else "/",
            "pushType": push_type,
        },
        "tag": f"article-{article_id}" if article_id else "default",
        "requireInteraction": push_type == "breaking",
        "renotify": push_type == "breaking",
        "vibrate": [200, 100, 200] if push_type == "breaking" else [100],
    }


def _build_apns_payload(article: dict, push_type: str = "realtime") -> dict:
    """构建 APNs (iOS) 的 payload"""
    title = article.get("title_cn") or article.get("title", "AI晨报")
    body = ""

    if push_type == "breaking":
        body = f"🔴 突发 · {article.get('ai_comment', '') or article.get('summary_cn', '') or article.get('summary', '')}"
    elif push_type == "daily_digest":
        body = article.get("summary", "")
    else:
        body = (
            article.get("ai_comment", "") or
            article.get("summary_cn", "") or
            article.get("summary", "")
        )

    if body and len(body) > 200:
        body = body[:197] + "..."

    article_id = article.get("id", "")

    aps = {
        "alert": {
            "title": title[:80],
            "body": body[:200],
        },
        "badge": 1,
        "thread-id": f"ai-brief-{push_type}",
        "category": "ARTICLE",
    }

    if push_type == "breaking":
        aps["sound"] = "default"
        aps["critical-alert"] = 1
    else:
        aps["sound"] = "default"

    return {
        "aps": aps,
        "articleId": article_id,
        "pushType": push_type,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Web Push (VAPID) 客户端
# ══════════════════════════════════════════════════════════════════════════════

class WebPushClient:
    """Web Push 推送客户端（基于 pywebpush）"""

    def __init__(self):
        self._available = False
        try:
            from pywebpush import webpush, WebPushException
            self._webpush = webpush
            self._WebPushException = WebPushException
            self._available = bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)
            if self._available:
                logger.info("📡 WebPush 客户端已就绪")
            else:
                logger.warning("⚠️  WebPush 未配置 VAPID 密钥，Web Push 不可用")
        except ImportError:
            logger.warning("⚠️  pywebpush 未安装，Web Push 不可用（pip install pywebpush）")

    @property
    def available(self) -> bool:
        return self._available

    def send(self, device: dict, payload: dict) -> bool:
        """向单个 Web Push 设备发送通知"""
        if not self._available:
            return False

        endpoint = device.get("endpoint", "")
        p256dh = device.get("p256dh", "")
        auth = device.get("auth", "")

        if not endpoint or not p256dh or not auth:
            logger.warning(f"设备 {device.get('id', '?')} 缺少 Web Push 密钥")
            return False

        subscription_info = {
            "endpoint": endpoint,
            "keys": {
                "p256dh": p256dh,
                "auth": auth,
            },
        }

        try:
            self._webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS,
                timeout=10,
            )
            return True

        except self._WebPushException as e:
            status_code = getattr(e, 'response', None)
            if status_code is not None and hasattr(status_code, 'status_code'):
                code = status_code.status_code
                if code == 410:
                    # 订阅已失效，清理
                    logger.info(f"🗑️  设备 {device.get('token', '?')} 订阅已失效 (410 Gone)，自动清理")
                    unregister_device(device.get("token", ""))
                elif code == 429:
                    logger.warning(f"⏳ Web Push 限流 (429)，请稍后重试")
                else:
                    logger.warning(f"❌ Web Push 失败 (HTTP {code}): {e}")
            else:
                logger.warning(f"❌ Web Push 失败: {e}")
            return False

        except Exception as e:
            logger.warning(f"❌ Web Push 异常: {e}")
            return False


# ══════════════════════════════════════════════════════════════════════════════
# APNs 客户端
# ══════════════════════════════════════════════════════════════════════════════

class APNsClient:
    """Apple Push Notification service 客户端"""

    def __init__(self):
        self._available = False
        self._client = None

        if not APNS_KEY_ID or not APNS_TEAM_ID or not APNS_AUTH_KEY_PATH:
            logger.info("ℹ️  APNs 未配置（需要 APNS_KEY_ID + APNS_TEAM_ID + APNS_AUTH_KEY_PATH）")
            return

        if not os.path.exists(APNS_AUTH_KEY_PATH):
            logger.warning(f"⚠️  APNs .p8 密钥文件不存在: {APNS_AUTH_KEY_PATH}")
            return

        try:
            from apns2.client import APNsClient as _APNsClient
            from apns2.payload import Payload
            from apns2.credentials import TokenCredentials

            self._token_credentials = TokenCredentials(
                auth_key_filepath=APNS_AUTH_KEY_PATH,
                auth_key_id=APNS_KEY_ID,
                team_id=APNS_TEAM_ID,
            )

            mode = "development" if APNS_USE_SANDBOX else "production"
            self._client = _APNsClient(
                credentials=self._token_credentials,
                use_sandbox=APNS_USE_SANDBOX,
                use_alternative_port=False,
            )
            self._available = True
            logger.info(f"🍎 APNs 客户端已就绪 (mode={mode}, bundle={APNS_BUNDLE_ID})")

        except ImportError:
            logger.warning("⚠️  apns2 未安装，APNs 不可用（pip install apns2）")
        except Exception as e:
            logger.warning(f"⚠️  APNs 客户端初始化失败: {e}")

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    def send(self, device: dict, payload: dict) -> bool:
        """向单个 iOS 设备发送 APNs 通知"""
        if not self._available:
            return False

        device_token = device.get("token", "")
        if not device_token:
            logger.warning(f"设备 {device.get('id', '?')} 缺少 iOS device token")
            return False

        try:
            from apns2.payload import Payload as _Payload

            # 将 dict payload 转为 apns2 Payload
            aps = payload.get("aps", {})
            alert = aps.get("alert", {})
            badge = aps.get("badge", 0)
            sound = aps.get("sound", "default")

            apns_payload = _Payload(
                alert=alert,
                badge=badge,
                sound=sound,
                thread_id=aps.get("thread-id"),
                category=aps.get("category"),
                custom=payload.get("custom", {}),
            )

            # 通过 apns2 发送
            topic = APNS_BUNDLE_ID
            self._client.push(
                token_hex=device_token,
                payload=apns_payload,
                topic=topic,
            )
            return True

        except Exception as e:
            error_str = str(e)
            if "BadDeviceToken" in error_str or "Unregistered" in error_str:
                logger.info(f"🗑️  iOS 设备 {device_token[:16]}... 已注销，自动清理")
                unregister_device(device_token)
            elif "TooManyRequests" in error_str or "TooManyProviderTokenUpdates" in error_str:
                logger.warning(f"⏳ APNs 限流，请稍后重试")
            else:
                logger.warning(f"❌ APNs 推送失败: {e}")
            return False


# ══════════════════════════════════════════════════════════════════════════════
# PushService — 统一推送服务
# ══════════════════════════════════════════════════════════════════════════════

class PushService:
    """统一推送服务：Web Push + APNs 双通道"""

    def __init__(self):
        self.webpush = WebPushClient()
        self.apns = APNsClient()

    @property
    def _any_available(self) -> bool:
        return self.webpush.available or self.apns.available

    # ── 实时推送 ────────────────────────────────────────────────────────────

    async def send_realtime(self, article: dict) -> int:
        """
        实时推送：新文章 → 推送给所有订阅用户。

        返回成功推送的设备数。
        """
        return self._send_to_all(
            article=article,
            push_type="realtime",
            skip_quiet_hours=False,
        )

    # ── 突发新闻 ────────────────────────────────────────────────────────────

    async def send_breaking(self, article: dict) -> int:
        """
        重要事件：融资 > $100M / 大厂发布 → 突破静音的 critical alert。

        返回成功推送的设备数。
        """
        return self._send_to_all(
            article=article,
            push_type="breaking",
            skip_quiet_hours=True,  # breaking 突破静音
        )

    # ── 每日精选 ────────────────────────────────────────────────────────────

    async def send_daily_digest(self, date_str: str, summary: str) -> int:
        """
        每日精选：早上 7 点汇总推送。

        返回成功推送的设备数。
        """
        article = {
            "id": f"digest_{date_str}",
            "title": f"AI晨报 · {date_str}",
            "summary": summary,
            "ai_comment": summary,
        }
        return self._send_to_all(
            article=article,
            push_type="daily_digest",
            skip_quiet_hours=True,  # 定时推送突破静音
        )

    # ── 消费队列 ────────────────────────────────────────────────────────────

    async def process_queue(self, limit: int = 50) -> int:
        """
        消费 notification_queue 表中待推送记录。

        返回成功推送的通知数。
        """
        if not self._any_available:
            logger.warning("⚠️  无可用的推送通道，跳过队列处理")
            return 0

        pending = get_pending_notifications(limit)
        if not pending:
            return 0

        logger.info(f"📨 开始处理 {len(pending)} 条待推送通知...")
        success_count = 0

        for notif in pending:
            qid = notif.get("id")
            article_id = notif.get("article_id", "")
            push_type = notif.get("push_type", "realtime")
            priority = notif.get("priority", "normal")

            # 获取文章详情
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM articles WHERE id = ?", (article_id,))
            article_row = cursor.fetchone()
            conn.close()

            if not article_row:
                logger.warning(f"文章 {article_id} 不存在，跳过推送")
                mark_notification_failed(qid)
                continue

            article = dict(article_row)

            # 检查静音时段
            if push_type != "breaking" and _is_quiet_hours():
                logger.debug(f"⏰ 静音时段，跳过 {push_type} 推送: {article_id}")
                continue  # 不标记为 sent，等待静音时段结束

            # 发送
            sent = self._send_to_all(
                article=article,
                push_type=push_type,
                skip_quiet_hours=(push_type == "breaking"),
            )

            if sent > 0:
                mark_notification_sent(qid)
                success_count += 1
                logger.info(f"  ✅ 通知 #{qid}: {article.get('title_cn', article.get('title', ''))[:40]} → {sent} 设备")
            else:
                # 如果是因为没有设备可推送，也标记为 sent
                devices = get_active_devices()
                if not devices:
                    mark_notification_sent(qid)  # 没有设备也标记完成
                else:
                    mark_notification_failed(qid)
                logger.debug(f"  ⚠️ 通知 #{qid}: 0 设备")

        logger.info(f"📨 队列处理完成: {success_count}/{len(pending)} 成功")
        return success_count

    # ── 内部方法 ────────────────────────────────────────────────────────────

    def _send_to_all(self, article: dict, push_type: str = "realtime",
                     skip_quiet_hours: bool = False) -> int:
        """向所有活跃设备发送推送通知"""
        if not self._any_available:
            return 0

        # 检查静音时段
        if not skip_quiet_hours and _is_quiet_hours():
            logger.debug(f"⏰ 静音时段，跳过 {push_type} 推送")
            return 0

        devices = get_active_devices()
        if not devices:
            logger.debug("📭 无活跃设备订阅")
            return 0

        # 构建 payload
        web_payload = _build_web_push_payload(article, push_type)
        apns_payload = _build_apns_payload(article, push_type)

        success = 0
        for device in devices:
            platform = device.get("platform", "web")
            user_id = device.get("user_id", "")

            # 频率限制
            if not _check_rate_limit(user_id):
                logger.debug(f"⏱️  用户 {user_id} 超出频率限制，跳过")
                continue

            sent = False
            if platform == "ios" and self.apns.available:
                sent = self.apns.send(device, apns_payload)
            elif platform in ("web", "android") and self.webpush.available:
                sent = self.webpush.send(device, web_payload)
            elif self.webpush.available:
                # Fallback: 非 ios 都走 Web Push
                sent = self.webpush.send(device, web_payload)

            if sent:
                _record_push(user_id)
                success += 1

        return success


# ══════════════════════════════════════════════════════════════════════════════
# 模块级便捷函数
# ══════════════════════════════════════════════════════════════════════════════

_service: Optional[PushService] = None


def get_push_service() -> PushService:
    """获取全局 PushService 实例（单例）"""
    global _service
    if _service is None:
        _service = PushService()
    return _service


# ── 自检 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    svc = get_push_service()
    print(f"Web Push: {'✅ 可用' if svc.webpush.available else '❌ 不可用'}")
    print(f"APNs:     {'✅ 可用' if svc.apns.available else '❌ 不可用'}")
    print(f"VAPID Public:  {VAPID_PUBLIC_KEY[:32]}..." if VAPID_PUBLIC_KEY else "VAPID Public:  (未配置)")
    print(f"静音时段:      {QUIET_HOURS_START}:00-{QUIET_HOURS_END}:00 (北京时间)")
    print(f"频率限制:      {MAX_PUSH_PER_USER_PER_15MIN}条/15分钟/用户")
