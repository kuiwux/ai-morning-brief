#!/usr/bin/env python3
"""
支付 API — Flask Blueprint（挂载到 server.py）

端点:
  POST /api/v2/payment/verify-iap     → iOS IAP receipt 验证
  POST /api/v2/payment/create-session → 创建 Stripe 支付会话
  POST /api/v2/payment/webhook       → Stripe webhook 接收
  GET  /api/v2/payment/subscription   → 获取当前订阅状态
  POST /api/v2/payment/cancel        → 取消自动续费
  GET  /api/v2/payment/plans          → 获取套餐列表
  GET  /api/v2/payment/history        → 支付历史
  POST /api/v2/payment/demo-complete  → [演示模式] 模拟完成支付
"""

import logging
from functools import wraps

from flask import Blueprint, request, jsonify, g

from payment_service import (
    verify_apple_receipt,
    create_stripe_session,
    handle_stripe_webhook,
    upgrade_membership,
    check_subscription_status,
    handle_subscription_expired,
    demo_complete_payment,
)
from payment_db import (
    PLANS, get_all_plans,
    get_payment_history as db_get_payment_history,
    cancel_subscription as db_cancel_subscription,
)
from payment_db import init_payment_db

logger = logging.getLogger("payment_api")

# ── Blueprint ───────────────────────────────────────────────────────────────
payment_bp = Blueprint("payment", __name__, url_prefix="/api/v2/payment")


# ── require_auth 包装 ──────────────────────────────────────────────────────

def require_auth(f):
    """
    JWT 认证装饰器（兼容 auth_api 的 require_auth）
    如果 auth_api 已加载，优先使用它的验证逻辑；否则使用简化版
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            from auth_api import require_auth as auth_require, auth_bp
            # 使用 auth_api 的 require_auth
            wrapped = auth_require(f)
            return wrapped(*args, **kwargs)
        except (ImportError, RuntimeError):
            # 降级：简单 token 验证
            from auth_service import verify_jwt
            token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()

            if not token:
                return jsonify({"error": "未提供认证令牌", "code": "NO_TOKEN"}), 401

            user_id = verify_jwt(token)
            if not user_id:
                return jsonify({"error": "令牌无效或已过期", "code": "INVALID_TOKEN"}), 401

            g.user_id = user_id
            return f(*args, **kwargs)

    return decorated


# ── API 端点 ────────────────────────────────────────────────────────────────

@payment_bp.route("/plans", methods=["GET"])
def api_get_plans():
    """
    获取套餐列表
    GET /api/v2/payment/plans

    Response:
    {
        "plans": [
            {
                "plan_id": "pro_monthly",
                "name": "Pro 月付",
                "price": 12,
                "price_cents": 1200,
                "period": "1 month",
                "period_days": 30
            },
            ...
        ],
        "free_trial_days": 3
    }
    """
    try:
        from payment_db import FREE_TRIAL_DAYS
        return jsonify({
            "plans": get_all_plans(),
            "free_trial_days": FREE_TRIAL_DAYS,
        })
    except Exception as e:
        logger.error(f"get-plans 错误: {e}", exc_info=True)
        return jsonify({"error": "获取套餐列表失败"}), 500


@payment_bp.route("/verify-iap", methods=["POST"])
@require_auth
def api_verify_iap():
    """
    iOS IAP receipt 验证
    POST /api/v2/payment/verify-iap
    Headers: Authorization: Bearer ***
    Body: { "receipt_data": "base64_encoded_receipt", "plan_id": "pro_monthly" }

    Response:
    {
        "status": "ok",
        "subscription": { ... },
        "environment": "production" | "sandbox",
        "message": "..."
    }
    """
    try:
        data = request.get_json(force=True)
        receipt_data = (data.get("receipt_data") or "").strip()
        plan_id = (data.get("plan_id") or "").strip()

        if not receipt_data:
            return jsonify({"error": "receipt_data 不能为空"}), 400

        # 验证 receipt
        result = verify_apple_receipt(receipt_data)

        if result["status"] != "ok":
            return jsonify(result), 400

        # 验证 product_id 与 plan_id 匹配
        subscription = result.get("subscription", {})
        verified_plan_id = subscription.get("plan_id", "")

        if plan_id and verified_plan_id and plan_id != verified_plan_id:
            logger.warning(f"plan_id 不匹配: request={plan_id}, receipt={verified_plan_id}")

        # 升级会员（使用 receipt 中的 plan_id 或请求中的 plan_id）
        final_plan_id = verified_plan_id or plan_id or "pro_monthly"
        original_txn_id = subscription.get("original_transaction_id", "")

        upgrade_membership(
            user_id=g.user_id,
            plan_id=final_plan_id,
            source="apple_iap",
            original_transaction_id=original_txn_id,
        )

        # 记录支付
        from payment_db import record_payment, PLANS
        plan = PLANS.get(final_plan_id, {})
        record_payment(
            user_id=g.user_id,
            plan_id=final_plan_id,
            amount=plan.get("price", 0),
            source="apple_iap",
            transaction_id=original_txn_id,
            receipt_data=receipt_data[:500],  # 截断存储
        )

        return jsonify({
            "status": "ok",
            "message": f"验证成功，已开通 {plan.get('name', final_plan_id)}",
            "environment": result["environment"],
            "subscription": subscription,
        })

    except Exception as e:
        logger.error(f"verify-iap 错误: {e}", exc_info=True)
        return jsonify({"error": "IAP 验证失败"}), 500


@payment_bp.route("/create-session", methods=["POST"])
@require_auth
def api_create_session():
    """
    创建 Stripe 支付会话
    POST /api/v2/payment/create-session
    Headers: Authorization: Bearer ***
    Body: { "plan_id": "pro_monthly", "success_url": "...", "cancel_url": "..." }

    演示模式下直接返回模拟结果
    """
    try:
        data = request.get_json(force=True)
        plan_id = (data.get("plan_id") or "").strip()
        success_url = (data.get("success_url") or "").strip() or None
        cancel_url = (data.get("cancel_url") or "").strip() or None

        if not plan_id:
            return jsonify({"error": "plan_id 不能为空"}), 400

        if plan_id not in PLANS:
            return jsonify({"error": f"无效的套餐: {plan_id}", "available": list(PLANS.keys())}), 400

        result = create_stripe_session(
            user_id=g.user_id,
            plan_id=plan_id,
            success_url=success_url,
            cancel_url=cancel_url,
        )

        return jsonify(result)

    except Exception as e:
        logger.error(f"create-session 错误: {e}", exc_info=True)
        return jsonify({"error": "创建支付会话失败"}), 500


@payment_bp.route("/webhook", methods=["POST"])
def api_webhook():
    """
    接收 Stripe Webhook
    POST /api/v2/payment/webhook
    Headers: Stripe-Signature: ...

    注意：此端点不需要 JWT 认证，使用 Stripe 签名验证
    """
    try:
        payload = request.get_data()
        signature = request.headers.get("Stripe-Signature", "")

        result = handle_stripe_webhook(payload, signature)
        status_code = 200 if result["status"] == "ok" else 400

        return jsonify(result), status_code

    except Exception as e:
        logger.error(f"webhook 错误: {e}", exc_info=True)
        return jsonify({"error": "Webhook 处理失败"}), 500


@payment_bp.route("/subscription", methods=["GET"])
@require_auth
def api_get_subscription():
    """
    获取当前订阅状态
    GET /api/v2/payment/subscription
    Headers: Authorization: Bearer ***

    Response:
    {
        "has_subscription": false,
        "is_trial": false,
        "plan_id": null,
        "status": "free",
        "expires_at": null,
        "days_remaining": null
    }
    """
    try:
        result = check_subscription_status(g.user_id)
        return jsonify({"status": "ok", **result})

    except Exception as e:
        logger.error(f"get-subscription 错误: {e}", exc_info=True)
        return jsonify({"error": "获取订阅状态失败"}), 500


@payment_bp.route("/cancel", methods=["POST"])
@require_auth
def api_cancel_subscription():
    """
    取消自动续费（到期后不续费，当前周期仍可用）
    POST /api/v2/payment/cancel
    Headers: Authorization: Bearer ***

    Response:
    { "status": "ok", "message": "已取消自动续费，当前订阅可使用至到期日" }
    """
    try:
        ok = db_cancel_subscription(g.user_id)
        if ok:
            # 同时获取最新状态
            sub_status = check_subscription_status(g.user_id)
            return jsonify({
                "status": "ok",
                "message": "已取消自动续费，当前订阅可使用至到期日",
                "subscription": sub_status,
            })
        else:
            return jsonify({
                "status": "ok",
                "message": "未找到活跃的订阅",
                "subscription": check_subscription_status(g.user_id),
            })

    except Exception as e:
        logger.error(f"cancel-subscription 错误: {e}", exc_info=True)
        return jsonify({"error": "取消失败"}), 500


@payment_bp.route("/history", methods=["GET"])
@require_auth
def api_get_history():
    """
    获取支付历史
    GET /api/v2/payment/history?limit=50
    Headers: Authorization: Bearer ***

    Response:
    {
        "payments": [
            {
                "id": 1,
                "user_id": "u_xxx",
                "plan_id": "pro_monthly",
                "amount": 12.0,
                "currency": "CNY",
                "source": "stripe",
                "transaction_id": "txn_xxx",
                "created_at": "2026-05-31T..."
            }
        ],
        "total": 1
    }
    """
    try:
        limit = request.args.get("limit", 50, type=int)
        limit = max(1, min(limit, 200))

        payments = db_get_payment_history(g.user_id, limit=limit)

        # 过滤敏感字段
        safe_payments = []
        for p in payments:
            safe = dict(p)
            safe.pop("receipt_data", None)
            safe_payments.append(safe)

        return jsonify({
            "payments": safe_payments,
            "total": len(safe_payments),
        })

    except Exception as e:
        logger.error(f"get-history 错误: {e}", exc_info=True)
        return jsonify({"error": "获取支付历史失败"}), 500


@payment_bp.route("/demo-complete", methods=["POST"])
def api_demo_complete():
    """
    [演示模式] 模拟完成支付（仅开发环境用）
    POST /api/v2/payment/demo-complete
    Body: { "user_id": "u_xxx", "plan_id": "pro_monthly" }
    """
    try:
        data = request.get_json(force=True)
        user_id = (data.get("user_id") or "").strip()
        plan_id = (data.get("plan_id") or "").strip()

        if not user_id:
            return jsonify({"error": "user_id 不能为空"}), 400
        if not plan_id:
            return jsonify({"error": "plan_id 不能为空"}), 400
        if plan_id not in PLANS:
            return jsonify({"error": f"无效的套餐: {plan_id}"}), 400

        result = demo_complete_payment(user_id, plan_id)
        return jsonify(result)

    except Exception as e:
        logger.error(f"demo-complete 错误: {e}", exc_info=True)
        return jsonify({"error": "演示支付失败"}), 500
