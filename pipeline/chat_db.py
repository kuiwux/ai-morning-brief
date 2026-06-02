#!/usr/bin/env python3
"""
对话数据层 — chat_history 表 + 每日配额表
"""
import os
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("chat_db")

# ── 数据库路径 ─────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")


def get_db() -> sqlite3.Connection:
    """获取数据库连接（每次请求新建，确保线程安全）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_chat_db() -> None:
    """初始化对话相关表"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ── 对话历史表 ──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            article_id TEXT,
            mode TEXT DEFAULT 'article',
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            tokens_used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── 每日配额表 ──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_daily_quota (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            query_date TEXT NOT NULL,
            query_count INTEGER DEFAULT 1,
            last_query_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, query_date)
        )
    """)

    # ── 索引 ──
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_history_v2_user
        ON chat_history_v2(user_id, created_at DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_history_v2_article
        ON chat_history_v2(user_id, article_id, created_at DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_daily_quota_user_date
        ON chat_daily_quota(user_id, query_date)
    """)

    conn.commit()
    conn.close()
    logger.info("📦 对话数据库表已初始化（chat_history_v2, chat_daily_quota）")


# ── 对话历史 CRUD ──────────────────────────────────────────────────────────

def save_chat(
    user_id: str,
    question: str,
    answer: str,
    article_id: Optional[str] = None,
    mode: str = "article",
    tokens_used: int = 0,
) -> int:
    """保存一条对话记录，返回记录 ID"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO chat_history_v2
               (user_id, article_id, mode, question, answer, tokens_used)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, article_id, mode, question, answer, tokens_used),
        )
        conn.commit()
        chat_id = cursor.lastrowid
        return chat_id
    except Exception as e:
        logger.error(f"保存对话失败: {e}")
        return 0
    finally:
        conn.close()


def get_chat_history(
    user_id: str,
    article_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """获取对话历史"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        if article_id:
            cursor.execute(
                """SELECT id, user_id, article_id, mode, question, answer,
                          tokens_used, created_at
                   FROM chat_history_v2
                   WHERE user_id = ? AND article_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (user_id, article_id, limit),
            )
        else:
            cursor.execute(
                """SELECT id, user_id, article_id, mode, question, answer,
                          tokens_used, created_at
                   FROM chat_history_v2
                   WHERE user_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (user_id, limit),
            )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"获取对话历史失败: {e}")
        return []
    finally:
        conn.close()


def clear_chat_history(user_id: str, article_id: Optional[str] = None) -> int:
    """清除对话历史，返回删除的行数"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        if article_id:
            cursor.execute(
                "DELETE FROM chat_history_v2 WHERE user_id = ? AND article_id = ?",
                (user_id, article_id),
            )
        else:
            cursor.execute(
                "DELETE FROM chat_history_v2 WHERE user_id = ?",
                (user_id,),
            )
        conn.commit()
        deleted = cursor.rowcount
        return deleted
    except Exception as e:
        logger.error(f"清除对话历史失败: {e}")
        return 0
    finally:
        conn.close()


# ── 每日配额 ────────────────────────────────────────────────────────────────

# Free 用户每日免费次数
FREE_DAILY_LIMIT = 10


def _beijing_today() -> str:
    """返回北京时间今天的日期字符串 YYYY-MM-DD"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


def check_daily_quota(user_id: str) -> dict:
    """
    检查用户今日配额
    返回 {"used": int, "limit": int, "remaining": int, "can_query": bool}
    Pro/Pro+ 用户返回 unlimited
    """
    today = _beijing_today()
    conn = get_db()
    cursor = conn.cursor()

    try:
        # 1. 查用户会员类型
        cursor.execute(
            "SELECT member_type FROM users WHERE id = ?",
            (user_id,),
        )
        user_row = cursor.fetchone()

        if user_row and user_row["member_type"] in ("pro", "pro_plus", "pro+"):
            return {
                "used": 0,
                "limit": float("inf"),
                "remaining": float("inf"),
                "can_query": True,
                "is_pro": True,
            }

        # 2. Free 用户查今日用量
        cursor.execute(
            "SELECT query_count FROM chat_daily_quota WHERE user_id = ? AND query_date = ?",
            (user_id, today),
        )
        quota_row = cursor.fetchone()

        used = quota_row["query_count"] if quota_row else 0
        remaining = max(0, FREE_DAILY_LIMIT - used)

        return {
            "used": used,
            "limit": FREE_DAILY_LIMIT,
            "remaining": remaining,
            "can_query": used < FREE_DAILY_LIMIT,
            "is_pro": False,
        }
    except Exception as e:
        logger.error(f"检查每日配额失败: {e}")
        # 出错时允许查询（降级）
        return {
            "used": 0,
            "limit": FREE_DAILY_LIMIT,
            "remaining": FREE_DAILY_LIMIT,
            "can_query": True,
            "is_pro": False,
        }
    finally:
        conn.close()


def increment_daily_quota(user_id: str) -> int:
    """
    增加今日查询次数计数
    返回新的已用次数
    """
    today = _beijing_today()
    conn = get_db()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """INSERT INTO chat_daily_quota (user_id, query_date, query_count, last_query_at)
               VALUES (?, ?, 1, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id, query_date)
               DO UPDATE SET query_count = query_count + 1,
                             last_query_at = CURRENT_TIMESTAMP""",
            (user_id, today),
        )
        conn.commit()

        # 读取更新后的计数
        cursor.execute(
            "SELECT query_count FROM chat_daily_quota WHERE user_id = ? AND query_date = ?",
            (user_id, today),
        )
        row = cursor.fetchone()
        return row["query_count"] if row else 0
    except Exception as e:
        logger.error(f"更新每日配额失败: {e}")
        return -1
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    init_chat_db()
    print("✅ 对话数据库表初始化完成")
