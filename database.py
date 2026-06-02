"""
Parro 用户系统 - 数据库模块
管理 SQLite 数据库的初始化、连接和用户表操作
"""

import sqlite3
import os
import bcrypt

# 数据库文件路径
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DB_PATH = os.path.join(DB_DIR, 'parro.db')


def get_db():
    """获取数据库连接，自动启用 WAL 模式和外键约束"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """初始化数据库表结构"""
    conn = get_db()
    cursor = conn.cursor()

    # 用户表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_demo INTEGER DEFAULT 0
        )
    ''')

    # 用户偏好设置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            topics TEXT DEFAULT '["AI","科技","产品","学术"]',
            language TEXT DEFAULT 'zh',
            push_frequency TEXT DEFAULT 'daily',
            push_enabled INTEGER DEFAULT 1,
            voice_id TEXT DEFAULT 'edge_yunxi',
            voice_speed REAL DEFAULT 1.0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    # Token 黑名单（用于登出等场景）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS token_blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_jti TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()
    print("[DB] 数据库初始化完成")


def create_demo_account():
    """
    预置 App Store 审核账号
    用户名: demo@parro.app
    密码: ParroDemo2026!
    """
    conn = get_db()
    cursor = conn.cursor()

    # 检查是否已存在
    cursor.execute("SELECT id FROM users WHERE username = ?", ("demo@parro.app",))
    existing = cursor.fetchone()

    if existing:
        print("[DB] 审核账号已存在，跳过创建")
        conn.close()
        return

    # 使用 bcrypt 哈希密码
    password_hash = bcrypt.hashpw(
        "ParroDemo2026!".encode('utf-8'),
        bcrypt.gensalt()
    ).decode('utf-8')

    cursor.execute(
        "INSERT INTO users (username, password_hash, is_demo) VALUES (?, ?, 1)",
        ("demo@parro.app", password_hash)
    )
    user_id = cursor.lastrowid

    # 创建默认偏好设置
    cursor.execute(
        "INSERT INTO user_preferences (user_id) VALUES (?)",
        (user_id,)
    )

    conn.commit()
    conn.close()
    print("[DB] 审核账号 demo@parro.app 创建成功")


# 模块加载时自动初始化
if __name__ != '__main__':
    init_db()
    create_demo_account()
