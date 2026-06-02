"""
Parro 用户系统 - 认证模块
JWT Token 生成/验证、密码哈希、装饰器
"""

import jwt
import bcrypt
import os
import time
from functools import wraps
from flask import request, jsonify, g

from database import get_db

# JWT 密钥（生产环境应从环境变量读取）
JWT_SECRET = os.environ.get('JWT_SECRET', 'parro-jwt-secret-key-2026-please-change-in-production')
JWT_ALGORITHM = 'HS256'
TOKEN_EXPIRE_HOURS = 72  # Token 有效期 72 小时


def hash_password(password: str) -> str:
    """使用 bcrypt 对密码进行哈希"""
    return bcrypt.hashpw(
        password.encode('utf-8'),
        bcrypt.gensalt()
    ).decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码是否匹配哈希值"""
    return bcrypt.checkpw(
        password.encode('utf-8'),
        password_hash.encode('utf-8')
    )


def create_token(user_id: int, username: str, name: str = None) -> str:
    """Generate JWT token, optionally including display name"""
    now = int(time.time())
    payload = {
        'sub': str(user_id),
        'username': username,
        'iat': now,
        'exp': now + TOKEN_EXPIRE_HOURS * 3600,
        'jti': f'{user_id}-{now}-{os.urandom(8).hex()}'
    }
    if name:
        payload['name'] = name
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    """解码并验证 JWT Token，返回 payload 或 None"""
    try:
        # 检查黑名单
        conn = get_db()
        cursor = conn.cursor()
        try:
            unverified = jwt.decode(token, algorithms=["HS256"], options={"verify_signature": False})
            jti = unverified.get('jti', '')
            cursor.execute(
                "SELECT id FROM token_blacklist WHERE token_jti = ?",
                (jti,)
            )
            if cursor.fetchone():
                return None
        except Exception:
            pass
        finally:
            conn.close()

        # 验证签名和过期时间
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def blacklist_token(token: str):
    """将 token 加入黑名单（用于登出）"""
    try:
        unverified = jwt.decode(token, algorithms=["HS256"], options={"verify_signature": False})
        jti = unverified.get('jti', '')
        exp = unverified.get('exp', int(time.time()) + 86400)

        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO token_blacklist (token_jti, expires_at) VALUES (?, ?)",
            (jti, exp)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def login_required(f):
    """
    装饰器：要求请求携带有效的 Bearer Token
    使用方式: @login_required
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        token = None

        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
        else:
            # 也支持 ?token=xxx 查询参数（方便调试）
            token = request.args.get('token', '')

        if not token:
            return jsonify({'error': '未提供认证令牌', 'code': 'UNAUTHORIZED'}), 401

        payload = decode_token(token)
        if not payload:
            return jsonify({'error': '令牌无效或已过期', 'code': 'TOKEN_INVALID'}), 401

        # 将用户信息注入到 Flask g 对象
        g.current_user_id = int(payload['sub'])
        g.current_username = payload['username']
        g.current_token = token

        return f(*args, **kwargs)

    return decorated


def get_current_user_id() -> int:
    """获取当前请求用户的 ID（在 @login_required 装饰器之后使用）"""
    return getattr(g, 'current_user_id', None)
