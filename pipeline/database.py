#!/usr/bin/env python3
"""
SQLite 数据层 — 资讯表、推送队列表、设备 Token 表
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("database")

# ── 数据库路径 ─────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")


def get_db() -> sqlite3.Connection:
    """获取数据库连接（每次请求创建新的，确保线程安全）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """初始化数据库，创建所有表"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ── 资讯表 ──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            title_cn TEXT,
            summary TEXT,
            summary_cn TEXT,
            ai_comment TEXT,
            eli5 TEXT,
            category TEXT,
            tags TEXT,
            source_type TEXT,
            source_region TEXT,
            source_url TEXT,
            credibility TEXT DEFAULT 'medium',
            source_count INTEGER DEFAULT 1,
            published_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_pushed INTEGER DEFAULT 0
        )
    """)

    # ── 推送队列表 ──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id TEXT NOT NULL,
            push_type TEXT DEFAULT 'realtime',
            priority TEXT DEFAULT 'normal',
            payload TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            FOREIGN KEY (article_id) REFERENCES articles(id)
        )
    """)

    # ── 设备 Token 表 ──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS device_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            token TEXT NOT NULL UNIQUE,
            platform TEXT DEFAULT 'web',
            endpoint TEXT,
            p256dh TEXT,
            auth TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP
        )
    """)

    # ── 索引 ──
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_articles_created
        ON articles(created_at DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_articles_category
        ON articles(category)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_articles_region
        ON articles(source_region)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_articles_source_url
        ON articles(source_url)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_queue_status
        ON notification_queue(status)
    """)

    conn.commit()
    conn.close()
    logger.info("📦 数据库表已初始化（articles, notification_queue, device_tokens）")


# ── 文章 CRUD ──────────────────────────────────────────────────────────────

def insert_article(article: dict) -> bool:
    """插入一篇文章，如果 id 已存在则跳过（返回 False）"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT OR IGNORE INTO articles (
                id, title, title_cn, summary, summary_cn, ai_comment, eli5,
                category, tags, source_type, source_region, source_url,
                credibility, source_count, published_at, is_pushed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            article.get("id", ""),
            article.get("title", ""),
            article.get("title_cn", ""),
            article.get("summary", ""),
            article.get("summary_cn", ""),
            article.get("ai_comment", ""),
            article.get("eli5", ""),
            article.get("category", ""),
            json.dumps(article.get("tags", []), ensure_ascii=False),
            article.get("source_type", ""),
            article.get("source_region", ""),
            article.get("source_url", ""),
            article.get("credibility", "medium"),
            article.get("source_count", 1),
            article.get("published_at", ""),
            article.get("is_pushed", 0),
        ))
        conn.commit()
        inserted = cursor.rowcount > 0
        return inserted
    except Exception as e:
        logger.error(f"插入文章失败: {e}")
        return False
    finally:
        conn.close()


def get_recent_article_urls(days: int = 3) -> set:
    """获取近 N 天内文章的 source_url 集合，用于去重"""
    conn = get_db()
    cursor = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        cursor.execute(
            "SELECT source_url FROM articles WHERE created_at > ?",
            (cutoff,)
        )
        urls = {row["source_url"] for row in cursor.fetchall() if row["source_url"]}
        return urls
    except Exception as e:
        logger.error(f"查询已有 URL 失败: {e}")
        return set()
    finally:
        conn.close()


def get_recent_article_ids(days: int = 3) -> set:
    """获取近 N 天内文章的 id 集合，用于去重"""
    conn = get_db()
    cursor = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        cursor.execute(
            "SELECT id FROM articles WHERE created_at > ?",
            (cutoff,)
        )
        return {row["id"] for row in cursor.fetchall()}
    except Exception as e:
        logger.error(f"查询已有 ID 失败: {e}")
        return set()
    finally:
        conn.close()


def get_recent_article_titles(days: int = 3) -> list[tuple]:
    """获取近 N 天内文章的 (id, title) 列表，用于标题相似度去重"""
    conn = get_db()
    cursor = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        cursor.execute(
            "SELECT id, title FROM articles WHERE created_at > ?",
            (cutoff,)
        )
        return [(row["id"], row["title"] or "") for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"查询已有标题失败: {e}")
        return []
    finally:
        conn.close()


def get_articles(
    category: str = None,
    region: str = None,
    since: str = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """查询文章列表，支持多条件筛选"""
    conn = get_db()
    cursor = conn.cursor()

    conditions = []
    params = []

    if category:
        conditions.append("category = ?")
        params.append(category)
    if region:
        conditions.append("source_region = ?")
        params.append(region)
    if since:
        conditions.append("created_at > ?")
        params.append(since)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # 总数
    cursor.execute(f"SELECT COUNT(*) FROM articles {where}", params)
    total = cursor.fetchone()[0]

    # 数据
    cursor.execute(
        f"SELECT * FROM articles {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    rows = cursor.fetchall()
    articles = [_row_to_dict(r) for r in rows]
    conn.close()
    return articles, total


def get_article_by_id(article_id: str) -> Optional[dict]:
    """获取单篇文章详情"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM articles WHERE id = ?", (article_id,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def search_articles(query: str, limit: int = 20) -> list[dict]:
    """全文搜索：在 title, title_cn, summary, summary_cn 中匹配关键词"""
    conn = get_db()
    cursor = conn.cursor()
    like = f"%{query}%"
    cursor.execute(
        """SELECT * FROM articles
           WHERE title LIKE ? OR title_cn LIKE ? OR summary LIKE ? OR summary_cn LIKE ?
           ORDER BY created_at DESC LIMIT ?""",
        (like, like, like, like, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_history_by_date(date_str: str) -> list[dict]:
    """按日期查询历史资讯"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM articles WHERE DATE(created_at) = DATE(?) ORDER BY created_at DESC",
        (date_str,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_stats() -> dict:
    """统计信息：总数/今日新增/分类分布/来源分布"""
    conn = get_db()
    cursor = conn.cursor()

    # 总数
    cursor.execute("SELECT COUNT(*) FROM articles")
    total = cursor.fetchone()[0]

    # 今日新增
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(*) FROM articles WHERE DATE(created_at) = DATE(?)", (today,))
    today_new = cursor.fetchone()[0]

    # 分类分布
    cursor.execute(
        "SELECT category, COUNT(*) as cnt FROM articles GROUP BY category ORDER BY cnt DESC"
    )
    category_dist = {row["category"]: row["cnt"] for row in cursor.fetchall()}

    # 来源分布
    cursor.execute(
        "SELECT source_type, COUNT(*) as cnt FROM articles GROUP BY source_type ORDER BY cnt DESC"
    )
    source_dist = {row["source_type"]: row["cnt"] for row in cursor.fetchall()}

    # 区域分布
    cursor.execute(
        "SELECT source_region, COUNT(*) as cnt FROM articles GROUP BY source_region ORDER BY cnt DESC"
    )
    region_dist = {row["source_region"]: row["cnt"] for row in cursor.fetchall()}

    conn.close()
    return {
        "total": total,
        "today_new": today_new,
        "category_distribution": category_dist,
        "source_distribution": source_dist,
        "region_distribution": region_dist,
    }


# ── 推送队列 ────────────────────────────────────────────────────────────────

def enqueue_notification(article_id: str, push_type: str = "realtime",
                         priority: str = "normal", payload: dict = None) -> int:
    """将文章加入推送队列，返回 queue id"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO notification_queue (article_id, push_type, priority, payload)
           VALUES (?, ?, ?, ?)""",
        (article_id, push_type, priority, json.dumps(payload or {}, ensure_ascii=False)),
    )
    conn.commit()
    qid = cursor.lastrowid
    conn.close()
    return qid


def get_pending_notifications(limit: int = 50) -> list[dict]:
    """获取待推送的通知"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM notification_queue WHERE status = 'pending' ORDER BY priority DESC, created_at ASC LIMIT ?",
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def mark_notification_sent(qid: int) -> None:
    """标记通知为已发送"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE notification_queue SET status = 'sent', sent_at = CURRENT_TIMESTAMP WHERE id = ?",
        (qid,),
    )
    conn.commit()
    conn.close()


def mark_notification_failed(qid: int) -> None:
    """标记通知为失败"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE notification_queue SET status = 'failed' WHERE id = ?",
        (qid,),
    )
    conn.commit()
    conn.close()


# ── 设备 Token ──────────────────────────────────────────────────────────────

def register_device(user_id: str, token: str, platform: str = "web",
                    endpoint: str = None, p256dh: str = None, auth: str = None) -> bool:
    """注册设备推送 Token"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT OR REPLACE INTO device_tokens
               (user_id, token, platform, endpoint, p256dh, auth, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (user_id, token, platform, endpoint, p256dh, auth),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"注册设备失败: {e}")
        return False
    finally:
        conn.close()


def unregister_device(token: str) -> bool:
    """注销设备推送（软删除，标记为 inactive）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE device_tokens SET is_active = 0 WHERE token = ?", (token,))
    conn.commit()
    affected = cursor.rowcount > 0
    conn.close()
    return affected


def get_active_devices() -> list[dict]:
    """获取所有活跃设备"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM device_tokens WHERE is_active = 1")
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


# ── 工具 ───────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    """将 sqlite3.Row 转为 dict，并解析 tags JSON"""
    d = dict(row)
    if "tags" in d and isinstance(d["tags"], str):
        try:
            d["tags"] = json.loads(d["tags"])
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
    return d


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    init_db()
    print("✅ 数据库初始化完成")
