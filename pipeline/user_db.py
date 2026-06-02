#!/usr/bin/env python3
"""
用户数据层 — 用户表 + 短信验证码表
"""

import os
import uuid
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("user_db")

# ── 数据库路径 ─────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")


def get_db() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_auth_db() -> None:
    """初始化认证相关表"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            phone TEXT UNIQUE,
            wechat_openid TEXT UNIQUE,
            nickname TEXT DEFAULT 'AI探索者',
            avatar_url TEXT,
            member_type TEXT DEFAULT 'free',
            is_guest INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sms_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            code TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_phone
        ON users(phone)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_wechat_openid
        ON users(wechat_openid)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sms_codes_phone
        ON sms_codes(phone, used, expires_at)
    """)

    conn.commit()
    conn.close()
    logger.info("📦 认证数据库表已初始化（users, sms_codes）")


# ── 用户 CRUD ──────────────────────────────────────────────────────────────

def create_user(
    phone: str = None,
    wechat_openid: str = None,
    nickname: str = "AI探索者",
    avatar_url: str = None,
    is_guest: bool = False,
) -> dict:
    """创建用户，返回用户 dict"""
    conn = get_db()
    cursor = conn.cursor()
    user_id = f"u_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc).isoformat()

    try:
        cursor.execute(
            """INSERT INTO users (id, phone, wechat_openid, nickname, avatar_url, is_guest, created_at, last_login)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, phone, wechat_openid, nickname, avatar_url, int(is_guest), now, now),
        )
        conn.commit()
        logger.info(f"✅ 用户创建: {user_id} (phone={phone}, wechat={wechat_openid}, guest={is_guest})")

        # 立即查询并返回（在连接关闭前）
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return _row_to_dict(row)

    except sqlite3.IntegrityError:
        # 用户已存在（phone/wechat_openid 冲突），查询已有用户
        conn.close()
        if phone:
            return get_user_by_phone(phone)
        if wechat_openid:
            return get_user_by_wechat_openid(wechat_openid)
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_user_by_id(user_id: str) -> Optional[dict]:
    """根据 id 查询用户"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def get_user_by_phone(phone: str) -> Optional[dict]:
    """根据手机号查询用户"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE phone = ?", (phone,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def get_user_by_wechat_openid(openid: str) -> Optional[dict]:
    """根据微信 openid 查询用户"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE wechat_openid = ?", (openid,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def update_user(
    user_id: str,
    nickname: str = None,
    avatar_url: str = None,
) -> Optional[dict]:
    """更新用户资料"""
    conn = get_db()
    cursor = conn.cursor()

    updates = []
    params = []
    if nickname is not None:
        updates.append("nickname = ?")
        params.append(nickname)
    if avatar_url is not None:
        updates.append("avatar_url = ?")
        params.append(avatar_url)

    if not updates:
        conn.close()
        return get_user_by_id(user_id)

    params.append(user_id)
    cursor.execute(
        f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
        params,
    )
    conn.commit()

    # 查询更新后的记录
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def update_last_login(user_id: str) -> None:
    """更新最后登录时间"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
    conn.commit()
    conn.close()


def bind_phone(user_id: str, phone: str) -> bool:
    """游客绑定手机号，升级为正式用户"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE users SET phone = ?, is_guest = 0 WHERE id = ? AND is_guest = 1",
            (phone, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


# ── 短信验证码 ─────────────────────────────────────────────────────────────

def save_sms_code(phone: str, code: str, ttl_minutes: int = 5) -> None:
    """保存短信验证码"""
    conn = get_db()
    cursor = conn.cursor()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
    cursor.execute(
        "INSERT INTO sms_codes (phone, code, expires_at) VALUES (?, ?, ?)",
        (phone, code, expires_at),
    )
    conn.commit()
    conn.close()
    logger.info(f"📱 验证码已生成: phone={phone}, code={code}")


def verify_sms_code(phone: str, code: str) -> bool:
    """验证短信验证码（使用后标记为已使用）"""
    conn = get_db()
    cursor = conn.cursor()

    # 查找未使用、未过期的验证码
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        """SELECT id FROM sms_codes
           WHERE phone = ? AND code = ? AND used = 0 AND expires_at > ?
           ORDER BY id DESC LIMIT 1""",
        (phone, code, now),
    )
    row = cursor.fetchone()

    if row:
        # 标记为已使用
        cursor.execute("UPDATE sms_codes SET used = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        conn.close()
        logger.info(f"✅ 验证码验证成功: phone={phone}")
        return True

    conn.close()
    logger.warning(f"❌ 验证码无效: phone={phone}, code={code}")
    return False


def cleanup_expired_sms() -> int:
    """清理过期的验证码记录"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute("DELETE FROM sms_codes WHERE expires_at < ?", (now,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted:
        logger.info(f"🧹 清理了 {deleted} 条过期验证码")
    return deleted


def cleanup_guest_users(days: int = 7) -> int:
    """清理 N 天未登录的游客账户"""
    conn = get_db()
    cursor = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cursor.execute(
        "DELETE FROM users WHERE is_guest = 1 AND last_login < ?",
        (cutoff,),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted:
        logger.info(f"🧹 清理了 {deleted} 个不活跃游客账户（>{days}天未登录）")
    return deleted


# ── 工具 ───────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    """将 sqlite3.Row 转为 dict"""
    if row is None:
        return {}
    d = dict(row)
    d["is_guest"] = bool(d.get("is_guest", 0))
    return d


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    init_auth_db()
    print("✅ 认证数据库初始化完成")
