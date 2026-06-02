#!/usr/bin/env python3
"""
支付数据层 — subscriptions + payment_history
"""

import os
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("payment_db")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_payment_db() -> None:
    """初始化支付相关表"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            source TEXT NOT NULL,
            original_transaction_id TEXT,
            stripe_subscription_id TEXT,
            status TEXT DEFAULT 'active',
            started_at TIMESTAMP,
            expires_at TIMESTAMP,
            auto_renew INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'CNY',
            source TEXT NOT NULL,
            transaction_id TEXT,
            receipt_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id
        ON subscriptions(user_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_subscriptions_status
        ON subscriptions(status)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_payment_history_user_id
        ON payment_history(user_id)
    """)

    conn.commit()
    conn.close()
    logger.info("📦 支付数据库表已初始化（subscriptions, payment_history）")


# ── 套餐定义 ────────────────────────────────────────────────────────────────

PLANS = {
    "pro_monthly":      {"name": "Pro 月付",  "price": 12,   "price_cents": 1200,  "period": "1 month",  "period_days": 30},
    "pro_yearly":       {"name": "Pro 年付",  "price": 88,   "price_cents": 8800,  "period": "1 year",   "period_days": 365},
    "pro_plus_monthly": {"name": "Pro+ 月付", "price": 25,   "price_cents": 2500,  "period": "1 month",  "period_days": 30},
    "pro_plus_yearly":  {"name": "Pro+ 年付", "price": 188,  "price_cents": 18800, "period": "1 year",   "period_days": 365},
}

FREE_TRIAL_DAYS = 3  # 新用户免费试用天数


def get_plan(plan_id: str) -> Optional[dict]:
    """获取套餐定义"""
    return PLANS.get(plan_id)


def get_all_plans() -> list[dict]:
    """获取所有套餐列表"""
    return [
        {"plan_id": pid, **info}
        for pid, info in PLANS.items()
    ]


# ── 订阅 CRUD ──────────────────────────────────────────────────────────────

def create_subscription(
    user_id: str,
    plan_id: str,
    source: str,
    original_transaction_id: str = None,
    stripe_subscription_id: str = None,
    status: str = "active",
    auto_renew: bool = True,
) -> dict:
    """创建订阅记录"""
    plan = PLANS.get(plan_id)
    if not plan:
        raise ValueError(f"无效的套餐: {plan_id}")

    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=plan["period_days"])

    cursor.execute(
        """INSERT INTO subscriptions
           (user_id, plan_id, source, original_transaction_id, stripe_subscription_id,
            status, started_at, expires_at, auto_renew)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id, plan_id, source,
            original_transaction_id, stripe_subscription_id,
            status, now.isoformat(), expires_at.isoformat(),
            int(auto_renew),
        ),
    )
    conn.commit()
    sub_id = cursor.lastrowid

    # 更新用户 member_type
    member_type = "pro_plus" if "plus" in plan_id else "pro"
    cursor.execute("UPDATE users SET member_type = ? WHERE id = ?", (member_type, user_id))
    conn.commit()

    cursor.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    row = cursor.fetchone()
    conn.close()

    logger.info(f"✅ 订阅创建: user={user_id}, plan={plan_id}, source={source}, id={sub_id}")
    return _row_to_dict(row)


def create_trial_subscription(user_id: str) -> Optional[dict]:
    """为新用户创建 3 天免费试用"""
    conn = get_db()
    cursor = conn.cursor()

    # 检查是否已有试用或付费订阅
    cursor.execute(
        "SELECT id FROM subscriptions WHERE user_id = ? AND status IN ('active', 'trial')",
        (user_id,),
    )
    existing = cursor.fetchone()
    if existing:
        conn.close()
        logger.info(f"用户 {user_id} 已有订阅记录，跳过试用")
        return None

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=FREE_TRIAL_DAYS)

    cursor.execute(
        """INSERT INTO subscriptions
           (user_id, plan_id, source, status, started_at, expires_at, auto_renew)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, "pro_monthly", "trial", "trial", now.isoformat(), expires_at.isoformat(), 0),
    )
    conn.commit()
    sub_id = cursor.lastrowid

    # 更新用户 member_type 为 pro（试用期内）
    cursor.execute("UPDATE users SET member_type = ? WHERE id = ?", ("pro", user_id))
    conn.commit()

    cursor.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    row = cursor.fetchone()
    conn.close()

    logger.info(f"🎁 免费试用创建: user={user_id}, expires={expires_at.isoformat()}, id={sub_id}")
    return _row_to_dict(row)


def get_active_subscription(user_id: str) -> Optional[dict]:
    """获取用户当前活跃订阅"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM subscriptions
           WHERE user_id = ? AND status IN ('active', 'trial', 'grace_period')
           ORDER BY created_at DESC LIMIT 1""",
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def get_subscription_history(user_id: str, limit: int = 20) -> list[dict]:
    """获取用户订阅历史"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM subscriptions
           WHERE user_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (user_id, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def expire_subscription(user_id: str) -> bool:
    """将用户订阅标记为过期，并降级为 free"""
    conn = get_db()
    cursor = conn.cursor()

    # 将 active/trial 状态的订阅标记为 expired
    cursor.execute(
        """UPDATE subscriptions SET status = 'expired', auto_renew = 0
           WHERE user_id = ? AND status IN ('active', 'trial', 'grace_period')""",
        (user_id,),
    )
    affected = cursor.rowcount

    # 降级用户 member_type
    cursor.execute("UPDATE users SET member_type = 'free' WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    logger.info(f"⏰ 订阅过期: user={user_id}, affected_rows={affected}")
    return affected > 0


def cancel_subscription(user_id: str) -> bool:
    """取消自动续费（不立即过期，到期后生效）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE subscriptions SET auto_renew = 0
           WHERE user_id = ? AND status IN ('active', 'trial')""",
        (user_id,),
    )
    affected = cursor.rowcount
    conn.commit()
    conn.close()

    logger.info(f"🔕 取消自动续费: user={user_id}, affected={affected}")
    return affected > 0


def check_and_expire_trials() -> list[str]:
    """检查并过期所有试用已结束的用户，返回被降级的用户 ID 列表"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    # 查找过期但未处理的试用订阅
    cursor.execute(
        """SELECT user_id FROM subscriptions
           WHERE status = 'trial' AND expires_at < ?""",
        (now,),
    )
    expired_users = [row["user_id"] for row in cursor.fetchall()]
    conn.close()

    for user_id in expired_users:
        expire_subscription(user_id)
        logger.info(f"⏰ 试用过期降级: user={user_id}")

    return expired_users


# ── 支付历史 ────────────────────────────────────────────────────────────────

def record_payment(
    user_id: str,
    plan_id: str,
    amount: float,
    source: str,
    currency: str = "CNY",
    transaction_id: str = None,
    receipt_data: str = None,
) -> dict:
    """记录支付历史"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO payment_history
           (user_id, plan_id, amount, currency, source, transaction_id, receipt_data)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, plan_id, amount, currency, source, transaction_id, receipt_data),
    )
    conn.commit()
    pid = cursor.lastrowid
    cursor.execute("SELECT * FROM payment_history WHERE id = ?", (pid,))
    row = cursor.fetchone()
    conn.close()

    logger.info(f"💰 支付记录: user={user_id}, plan={plan_id}, amount={amount} {currency}, source={source}")
    return _row_to_dict(row)


def get_payment_history(user_id: str, limit: int = 50) -> list[dict]:
    """获取支付历史"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM payment_history
           WHERE user_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (user_id, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


# ── 工具 ───────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    d["auto_renew"] = bool(d.get("auto_renew", 0))
    return d


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    init_payment_db()
    print("✅ 支付数据库初始化完成")
    print("套餐列表:")
    for p in get_all_plans():
        print(f"  {p['plan_id']}: {p['name']} ¥{p['price']}/{p['period']}")
