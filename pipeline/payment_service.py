#!/usr/bin/env python3
"""
支付服务
- iOS：App Store IAP（StoreKit receipt 验证）
- PWA/安卓：Stripe Checkout（备选方案）

环境变量:
  STRIPE_SECRET_KEY    — Stripe 密钥（未配置时降级为演示模式）
  STRIPE_WEBHOOK_SECRET — Stripe webhook 签名密钥
  APPLE_SHARED_SECRET  — App Store Connect 共享密钥
  APP_BUNDLE_ID        — iOS App Bundle ID
  BASE_URL             — 服务端基础 URL（用于 Stripe 回调）
"""

import os
import json
import logging
import time
from typing import Optional

import requests
import httpx

from payment_db import (
    PLANS, FREE_TRIAL_DAYS,
    create_subscription, create_trial_subscription,
    get_active_subscription, get_subscription_history,
    expire_subscription, cancel_subscription as db_cancel,
    record_payment, get_payment_history, check_and_expire_trials,
)

logger = logging.getLogger("payment_service")

# ── 配置 ───────────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APPLE_SHARED_SECRET = os.environ.get("APPLE_SHARED_SECRET", "")
APP_BUNDLE_ID = os.environ.get("APP_BUNDLE_ID", "com.aimorning.brief")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8898")

DEMO_MODE = not STRIPE_SECRET_KEY

if DEMO_MODE:
    logger.warning("⚠️  STRIPE_SECRET_KEY 未配置，Stripe 进入演示模式")
else:
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
    except ImportError:
        logger.error("❌ stripe 库未安装，运行: pip install stripe")
        DEMO_MODE = True


# ── Apple IAP 验证 ─────────────────────────────────────────────────────────

APPLE_PRODUCTION_URL = "https://buy.itunes.apple.com/verifyReceipt"
APPLE_SANDBOX_URL = "https://sandbox.itunes.apple.com/verifyReceipt"


def verify_apple_receipt(receipt_data: str) -> dict:
    """
    向 Apple 验证 IAP receipt
    优先查生产环境，返回 21007 时自动重试沙盒环境

    Args:
        receipt_data: Base64 编码的 receipt

    Returns:
        {
            "status": "ok" | "error",
            "subscription": { ... } | None,
            "environment": "production" | "sandbox",
            "message": "..."
        }
    """
    if not APPLE_SHARED_SECRET:
        return {
            "status": "error",
            "message": "APPLE_SHARED_SECRET 未配置",
            "environment": None,
            "subscription": None,
        }

    payload = {
        "receipt-data": receipt_data,
        "password": APPLE_SHARED_SECRET,
        "exclude-old-transactions": True,
    }

    # Step 1: 生产环境验证
    for attempt, url in enumerate([APPLE_PRODUCTION_URL, APPLE_SANDBOX_URL]):
        try:
            resp = requests.post(url, json=payload, timeout=30)
            result = resp.json()

            status_code = result.get("status", -1)

            if status_code == 0:
                # 验证成功
                subscription = _parse_apple_receipt(result)
                env = "production" if attempt == 0 else "sandbox"
                logger.info(f"✅ Apple receipt 验证成功 (env={env})")
                return {
                    "status": "ok",
                    "environment": env,
                    "subscription": subscription,
                    "message": "验证成功",
                }

            elif status_code == 21007 and attempt == 0:
                # 沙盒 receipt 发送到了生产环境，重试沙盒
                logger.info("🔄 Apple receipt 是沙盒数据，切换到沙盒环境验证")
                continue

            else:
                # 其他错误
                error_msg = _apple_error_message(status_code)
                logger.warning(f"❌ Apple 验证失败: status={status_code}, msg={error_msg}")
                return {
                    "status": "error",
                    "environment": "production" if attempt == 0 else "sandbox",
                    "subscription": None,
                    "message": f"验证失败: {error_msg}",
                    "apple_status": status_code,
                }

        except requests.RequestException as e:
            logger.error(f"❌ Apple 验证请求异常: {e}")
            if attempt == 0:
                continue  # 尝试沙盒
            return {
                "status": "error",
                "message": f"网络请求失败: {e}",
                "environment": None,
                "subscription": None,
            }

    return {
        "status": "error",
        "message": "验证失败：所有环境均不可用",
        "environment": None,
        "subscription": None,
    }


def _parse_apple_receipt(result: dict) -> Optional[dict]:
    """解析 Apple receipt 响应中的订阅信息"""
    receipt = result.get("receipt", {})
    in_app = receipt.get("in_app", [])
    latest_info = result.get("latest_receipt_info", [])

    # 优先使用 latest_receipt_info
    transactions = latest_info if latest_info else in_app
    if not transactions:
        return None

    # 取最新的交易
    latest = transactions[-1]

    # 查找 product_id 对应的套餐
    product_id = latest.get("product_id", "")
    plan_id = _apple_product_to_plan(product_id)

    expires_date_ms = latest.get("expires_date_ms")
    expires_at = None
    if expires_date_ms:
        from datetime import datetime, timezone
        expires_at = datetime.fromtimestamp(int(expires_date_ms) / 1000, tz=timezone.utc).isoformat()

    return {
        "original_transaction_id": latest.get("original_transaction_id", ""),
        "transaction_id": latest.get("transaction_id", ""),
        "product_id": product_id,
        "plan_id": plan_id,
        "expires_at": expires_at,
        "is_trial_period": latest.get("is_trial_period", "false") == "true",
        "auto_renew": latest.get("auto_renew_status", "0") == "1",
    }


def _apple_product_to_plan(product_id: str) -> str:
    """将 Apple product_id 映射到 plan_id"""
    mapping = {
        "pro_monthly": "pro_monthly",
        "pro_yearly": "pro_yearly",
        "pro_plus_monthly": "pro_plus_monthly",
        "pro_plus_yearly": "pro_plus_yearly",
        # 常见命名格式
        f"{APP_BUNDLE_ID}.pro.monthly": "pro_monthly",
        f"{APP_BUNDLE_ID}.pro.yearly": "pro_yearly",
        f"{APP_BUNDLE_ID}.pro_plus.monthly": "pro_plus_monthly",
        f"{APP_BUNDLE_ID}.pro_plus.yearly": "pro_plus_yearly",
    }
    return mapping.get(product_id, "pro_monthly")


def _apple_error_message(status_code: int) -> str:
    """Apple 验证错误码说明"""
    messages = {
        0: "有效",
        21000: "App Store 无法读取 JSON",
        21002: "receipt-data 格式错误",
        21003: "receipt 认证失败",
        21004: "共享密钥不匹配",
        21005: "receipt 服务器不可用",
        21007: "沙盒 receipt 发送到生产环境",
        21008: "生产 receipt 发送到沙盒环境",
        21010: "receipt 已过期/已撤销",
    }
    return messages.get(status_code, f"未知错误 (status={status_code})")


# ── Stripe 支付 ─────────────────────────────────────────────────────────────

def create_stripe_session(user_id: str, plan_id: str, success_url: str = None, cancel_url: str = None) -> dict:
    """
    创建 Stripe Checkout Session

    Args:
        user_id: 用户 ID
        plan_id: 套餐 ID
        success_url: 支付成功跳转 URL
        cancel_url: 取消支付跳转 URL

    Returns:
        { "status": "ok"|"error", "url": "..."|None, "session_id": "..."|None, "message": "..." }
    """
    plan = PLANS.get(plan_id)
    if not plan:
        return {"status": "error", "message": f"无效的套餐: {plan_id}", "url": None, "session_id": None}

    if DEMO_MODE:
        # 演示模式：模拟成功
        demo_session_id = f"demo_cs_{user_id}_{plan_id}_{int(time.time())}"
        logger.info(f"🎭 [演示模式] Stripe Checkout 创建: user={user_id}, plan={plan_id}, session={demo_session_id}")
        return {
            "status": "ok",
            "message": f"[演示模式] 已模拟创建支付会话，套餐: {plan['name']} ¥{plan['price']}",
            "url": f"{BASE_URL}/api/v2/payment/demo-success?session_id={demo_session_id}&user_id={user_id}&plan_id={plan_id}",
            "session_id": demo_session_id,
        }

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card", "alipay", "wechat_pay"],
            line_items=[{
                "price_data": {
                    "currency": "cny",
                    "product_data": {
                        "name": f"硅谷AI晨报 - {plan['name']}",
                        "description": f"AI晨报 {plan['name']}，{plan['period']}订阅",
                    },
                    "unit_amount": plan["price_cents"],
                    "recurring": {
                        "interval": "month" if plan["period_days"] <= 31 else "year",
                        "interval_count": 1,
                    },
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=success_url or f"{BASE_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=cancel_url or f"{BASE_URL}/payment/cancel",
            client_reference_id=user_id,
            metadata={
                "user_id": user_id,
                "plan_id": plan_id,
            },
        )

        logger.info(f"💳 Stripe Checkout 创建: user={user_id}, plan={plan_id}, session={checkout_session.id}")
        return {
            "status": "ok",
            "url": checkout_session.url,
            "session_id": checkout_session.id,
            "message": f"支付链接已创建，套餐: {plan['name']} ¥{plan['price']}",
        }

    except Exception as e:
        logger.error(f"❌ Stripe 创建会话失败: {e}", exc_info=True)
        return {"status": "error", "message": f"创建支付会话失败: {e}", "url": None, "session_id": None}


def handle_stripe_webhook(payload: bytes, signature: str) -> dict:
    """
    处理 Stripe Webhook 事件

    Args:
        payload: 请求体原始字节
        signature: Stripe-Signature header

    Returns:
        { "status": "ok"|"error", "event": "...", "message": "..." }
    """
    if DEMO_MODE:
        logger.info(f"🎭 [演示模式] Stripe webhook 收到")
        return {"status": "ok", "event": "demo_webhook", "message": "[演示模式] Webhook 已接收"}

    if not STRIPE_WEBHOOK_SECRET:
        return {"status": "error", "message": "STRIPE_WEBHOOK_SECRET 未配置", "event": None}

    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return {"status": "error", "message": "无效的 payload", "event": None}
    except stripe.error.SignatureVerificationError:
        return {"status": "error", "message": "签名验证失败", "event": None}

    event_type = event["type"]
    logger.info(f"📨 Stripe webhook: {event_type}")

    if event_type == "checkout.session.completed":
        return _handle_checkout_completed(event)

    elif event_type == "invoice.paid":
        return _handle_invoice_paid(event)

    elif event_type == "invoice.payment_failed":
        return _handle_payment_failed(event)

    elif event_type == "customer.subscription.deleted":
        return _handle_subscription_deleted(event)

    elif event_type == "customer.subscription.updated":
        return _handle_subscription_updated(event)

    return {"status": "ok", "event": event_type, "message": f"收到事件: {event_type}"}


def _handle_checkout_completed(event: dict) -> dict:
    """处理 checkout.session.completed"""
    session = event["data"]["object"]
    user_id = session.get("client_reference_id") or session.get("metadata", {}).get("user_id", "")
    plan_id = session.get("metadata", {}).get("plan_id", "")
    subscription_id = session.get("subscription", "")

    if not user_id or not plan_id:
        return {"status": "error", "message": "缺少 user_id 或 plan_id", "event": "checkout.session.completed"}

    plan = PLANS.get(plan_id)
    if not plan:
        return {"status": "error", "message": f"无效的套餐: {plan_id}", "event": "checkout.session.completed"}

    amount = session.get("amount_total", 0) / 100.0  # Stripe amount 是分，转为元
    currency = session.get("currency", "cny").upper()

    # 记录支付
    record_payment(user_id, plan_id, amount, "stripe", currency=currency,
                   transaction_id=session.get("id"))

    # 创建/更新订阅
    upgrade_membership(user_id, plan_id, "stripe",
                       stripe_subscription_id=subscription_id)

    logger.info(f"✅ Stripe 支付完成: user={user_id}, plan={plan_id}, amount={amount} {currency}")
    return {
        "status": "ok",
        "event": "checkout.session.completed",
        "message": f"支付成功，已开通 {plan['name']}",
    }


def _handle_invoice_paid(event: dict) -> dict:
    """处理 invoice.paid（续费成功）"""
    invoice = event["data"]["object"]
    subscription_id = invoice.get("subscription", "")
    user_id = invoice.get("customer_metadata", {}).get("user_id", "") or invoice.get("metadata", {}).get("user_id", "")

    if subscription_id and user_id:
        # 更新订阅到期时间
        _update_stripe_subscription_expiry(user_id, subscription_id)

    return {"status": "ok", "event": "invoice.paid", "message": "续费成功"}


def _handle_payment_failed(event: dict) -> dict:
    """处理 invoice.payment_failed（续费失败）"""
    invoice = event["data"]["object"]
    subscription_id = invoice.get("subscription", "")
    user_id = invoice.get("customer_metadata", {}).get("user_id", "") or invoice.get("metadata", {}).get("user_id", "")

    if user_id:
        # 进入宽限期
        from payment_db import get_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE subscriptions SET status = 'grace_period' WHERE user_id = ? AND stripe_subscription_id = ?",
            (user_id, subscription_id),
        )
        conn.commit()
        conn.close()

    return {"status": "ok", "event": "invoice.payment_failed", "message": "续费失败，已进入宽限期"}


def _handle_subscription_deleted(event: dict) -> dict:
    """处理 customer.subscription.deleted（订阅取消）"""
    subscription = event["data"]["object"]
    subscription_id = subscription.get("id", "")
    user_id = subscription.get("metadata", {}).get("user_id", "")

    if user_id:
        from payment_db import get_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE subscriptions SET status = 'cancelled' WHERE user_id = ? AND stripe_subscription_id = ?",
            (user_id, subscription_id),
        )
        conn.commit()
        conn.close()

    return {"status": "ok", "event": "customer.subscription.deleted", "message": "订阅已取消"}


def _handle_subscription_updated(event: dict) -> dict:
    """处理 customer.subscription.updated"""
    subscription = event["data"]["object"]
    subscription_id = subscription.get("id", "")
    user_id = subscription.get("metadata", {}).get("user_id", "")

    if user_id and subscription_id:
        _update_stripe_subscription_expiry(user_id, subscription_id)

    return {"status": "ok", "event": "customer.subscription.updated", "message": "订阅已更新"}


def _update_stripe_subscription_expiry(user_id: str, subscription_id: str):
    """同步 Stripe 订阅到期时间到本地"""
    try:
        stripe_sub = stripe.Subscription.retrieve(subscription_id)
        current_period_end = stripe_sub.get("current_period_end", 0)

        if current_period_end:
            from datetime import datetime, timezone
            expires_at = datetime.fromtimestamp(current_period_end, tz=timezone.utc).isoformat()

            from payment_db import get_db
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE subscriptions SET expires_at = ?, status = 'active' WHERE user_id = ? AND stripe_subscription_id = ?",
                (expires_at, user_id, subscription_id),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"更新 Stripe 订阅到期时间失败: {e}")


# ── 会员管理 ────────────────────────────────────────────────────────────────

def upgrade_membership(user_id: str, plan_id: str, source: str,
                       original_transaction_id: str = None,
                       stripe_subscription_id: str = None) -> bool:
    """
    升级会员（购买后调用）

    Returns:
        True if successful, False otherwise
    """
    plan = PLANS.get(plan_id)
    if not plan:
        logger.error(f"升级会员失败: 无效的套餐 {plan_id}")
        return False

    try:
        # 先过期旧订阅
        from payment_db import get_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE subscriptions SET status = 'expired' WHERE user_id = ? AND status IN ('active', 'trial', 'grace_period')",
            (user_id,),
        )
        conn.commit()
        conn.close()

        # 创建新订阅
        create_subscription(
            user_id=user_id,
            plan_id=plan_id,
            source=source,
            original_transaction_id=original_transaction_id,
            stripe_subscription_id=stripe_subscription_id,
        )

        logger.info(f"⬆️ 会员升级: user={user_id}, plan={plan_id}, source={source}")
        return True

    except Exception as e:
        logger.error(f"升级会员失败: {e}", exc_info=True)
        return False


def check_subscription_status(user_id: str) -> dict:
    """
    检查用户当前订阅状态

    Returns:
        {
            "has_subscription": bool,
            "is_trial": bool,
            "plan_id": str or None,
            "plan_name": str or None,
            "status": str,  # free / trial / active / expired / cancelled
            "expires_at": str or None,
            "auto_renew": bool,
            "days_remaining": int or None,
        }
    """
    # 先检查并处理过期试用
    check_and_expire_trials()

    sub = get_active_subscription(user_id)

    if not sub:
        return {
            "has_subscription": False,
            "is_trial": False,
            "plan_id": None,
            "plan_name": None,
            "status": "free",
            "expires_at": None,
            "auto_renew": False,
            "days_remaining": None,
        }

    plan = PLANS.get(sub.get("plan_id", ""), {})

    # 计算剩余天数
    days_remaining = None
    expires_at = sub.get("expires_at")
    if expires_at:
        try:
            from datetime import datetime, timezone
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = exp - now
            days_remaining = max(0, delta.days)
        except Exception:
            pass

    is_trial = sub.get("status") == "trial"

    return {
        "has_subscription": True,
        "is_trial": is_trial,
        "plan_id": sub.get("plan_id"),
        "plan_name": plan.get("name", sub.get("plan_id", "")),
        "status": sub.get("status", "active"),
        "expires_at": expires_at,
        "auto_renew": sub.get("auto_renew", False),
        "days_remaining": days_remaining,
    }


def handle_subscription_expired(user_id: str) -> bool:
    """
    处理订阅过期 — 降级用户为 Free

    Returns:
        True if the user was downgraded, False otherwise
    """
    return expire_subscription(user_id)


def demo_complete_payment(user_id: str, plan_id: str) -> dict:
    """
    演示模式：模拟完成支付（用于开发测试）

    Returns payment result dict
    """
    plan = PLANS.get(plan_id)
    if not plan:
        return {"status": "error", "message": f"无效的套餐: {plan_id}"}

    # 记录支付
    transaction_id = f"demo_txn_{user_id}_{plan_id}_{int(time.time())}"
    record_payment(user_id, plan_id, plan["price"], "demo",
                   transaction_id=transaction_id)

    # 升级会员
    upgrade_membership(user_id, plan_id, "demo",
                       original_transaction_id=transaction_id)

    return {
        "status": "ok",
        "message": f"[演示模式] 支付成功，已开通 {plan['name']}",
        "plan": plan,
        "transaction_id": transaction_id,
    }


# ── 定时维护 ────────────────────────────────────────────────────────────────

def maintenance_check_subscriptions() -> dict:
    """
    定期检查订阅状态
    - 过期试用降级
    - 过期付费订阅降级

    Returns dict with stats
    """
    from payment_db import get_db

    trials_expired = check_and_expire_trials()

    # 检查过期付费订阅
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    cursor.execute(
        """SELECT user_id FROM subscriptions
           WHERE status = 'active' AND expires_at < ? AND auto_renew = 0""",
        (now,),
    )
    expired_paid = [row["user_id"] for row in cursor.fetchall()]
    conn.close()

    for user_id in expired_paid:
        expire_subscription(user_id)
        logger.info(f"⏰ 付费订阅过期降级: user={user_id}")

    return {
        "trials_expired": len(trials_expired),
        "paid_expired": len(expired_paid),
    }


# 引用 datetime 用于 maintenance 函数
from datetime import datetime, timezone
