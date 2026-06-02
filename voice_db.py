"""
语音数据持久化层
SQLite 存储克隆语音信息
"""

import os
import sqlite3
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── 数据库路径 ─────────────────────────────────────────────────────────────
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "parro.db")


def get_db() -> sqlite3.Connection:
    """获取数据库连接"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_voice_db() -> None:
    """初始化语音相关表"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cloned_voices (
            id TEXT PRIMARY KEY,
            user_id TEXT DEFAULT 'default',
            name TEXT NOT NULL,
            fish_voice_id TEXT,
            source_audio_path TEXT,
            status TEXT DEFAULT 'training',
            preview_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 索引
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_cloned_voices_user
        ON cloned_voices(user_id, created_at DESC)
    """)

    conn.commit()
    conn.close()
    logger.info("📦 语音数据表已初始化（cloned_voices）")


# ── CRUD ──────────────────────────────────────────────────────────────────


def insert_cloned_voice(
    voice_id: str,
    name: str,
    fish_voice_id: str = "",
    source_audio_path: str = "",
    status: str = "training",
    preview_url: str = "",
    user_id: str = "default",
) -> bool:
    """插入克隆语音记录"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT OR REPLACE INTO cloned_voices
               (id, user_id, name, fish_voice_id, source_audio_path, status, preview_url, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (voice_id, user_id, name, fish_voice_id, source_audio_path, status, preview_url),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"插入语音记录失败: {e}")
        return False
    finally:
        conn.close()


def get_cloned_voice(voice_id: str) -> Optional[dict]:
    """获取单个克隆语音记录"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cloned_voices WHERE id = ?", (voice_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_cloned_voices(user_id: str = "default") -> list[dict]:
    """获取用户的克隆语音列表"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM cloned_voices WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def update_voice_status(voice_id: str, status: str, fish_voice_id: str = "") -> bool:
    """更新语音状态"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        if fish_voice_id:
            cursor.execute(
                """UPDATE cloned_voices
                   SET status = ?, fish_voice_id = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (status, fish_voice_id, voice_id),
            )
        else:
            cursor.execute(
                """UPDATE cloned_voices
                   SET status = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (status, voice_id),
            )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"更新语音状态失败: {e}")
        return False
    finally:
        conn.close()


def delete_cloned_voice(voice_id: str, user_id: str = "default") -> bool:
    """删除克隆语音记录"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM cloned_voices WHERE id = ? AND user_id = ?",
            (voice_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"删除语音记录失败: {e}")
        return False
    finally:
        conn.close()


# ── 初始化 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    init_voice_db()
    print("✅ 语音数据库初始化完成")
