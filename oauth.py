"""
Parro OAuth module — Google, Apple, WeChat & Alipay Sign-In
Handles OAuth login flows and user lookup/creation.
"""

import os
import time
import json
import base64
import requests
from urllib.parse import urlencode

from flask import request, jsonify
from auth import create_token, decode_token
from database import get_db

# =============================================================================
#  Helper: create or get user by OAuth identity
# =============================================================================

def create_or_get_oauth_user(provider, oauth_id, email, name, avatar_url=None):
    """
    Look up an existing user by oauth_provider + oauth_id, or create a new one.
    Returns (user_id, username, is_new).
    """
    conn = get_db()
    try:
        # Try to find existing OAuth-linked user
        cursor = conn.execute(
            "SELECT id, username, name, email, avatar_url FROM users WHERE oauth_provider = ? AND oauth_id = ?",
            (provider, oauth_id)
        )
        row = cursor.fetchone()

        if row:
            # Update profile info if changed (email, name, avatar)
            updates = []
            params = []
            if name and row['name'] != name:
                updates.append("name = ?")
                params.append(name)
            if email and row['email'] != email:
                updates.append("email = ?")
                params.append(email)
            if avatar_url and row['avatar_url'] != avatar_url:
                updates.append("avatar_url = ?")
                params.append(avatar_url)
            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.extend([row['id']])
                conn.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
                    params
                )
                conn.commit()
            return row['id'], row['username'], False

        # Check if email is already used by another account (for linking)
        username = f"{provider}_{oauth_id}"
        if email:
            existing = conn.execute(
                "SELECT id FROM users WHERE email = ? AND oauth_provider IS NULL",
                (email,)
            ).fetchone()
            if existing:
                # Link OAuth to existing account
                conn.execute(
                    "UPDATE users SET oauth_provider = ?, oauth_id = ?, name = COALESCE(?, name), avatar_url = COALESCE(?, avatar_url), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (provider, oauth_id, name, avatar_url, existing['id'])
                )
                conn.commit()
                user_row = conn.execute(
                    "SELECT id, username FROM users WHERE id = ?",
                    (existing['id'],)
                ).fetchone()
                return user_row['id'], user_row['username'], False

        # Create new user
        cursor = conn.execute(
            """INSERT INTO users (username, email, name, avatar_url, oauth_provider, oauth_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (username, email, name, avatar_url, provider, oauth_id)
        )
        user_id = cursor.lastrowid

        # Create default preferences
        conn.execute(
            "INSERT INTO user_preferences (user_id) VALUES (?)",
            (user_id,)
        )
        conn.commit()

        return user_id, username, True

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
#  Google OAuth
# =============================================================================

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
GOOGLE_REDIRECT_URI = os.environ.get('GOOGLE_REDIRECT_URI', '')

GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'


def google_auth_url():
    """
    GET /api/auth/google/url
    Returns the Google OAuth authorization URL that the frontend should redirect to.
    """
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        return jsonify({
            'error': 'Google OAuth is not configured',
            'code': 'OAUTH_NOT_CONFIGURED'
        }), 500

    # Build authorization URL
    params = {
        'client_id': GOOGLE_CLIENT_ID,
        'redirect_uri': GOOGLE_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'openid email profile',
        'access_type': 'offline',
        'prompt': 'consent',
    }
    query = '&'.join(f'{k}={requests.utils.quote(v)}' for k, v in params.items())
    auth_url = f'{GOOGLE_AUTH_URL}?{query}'

    return jsonify({
        'url': auth_url,
        'provider': 'google'
    }), 200


def google_auth_callback():
    """
    GET /api/auth/google/callback?code=xxx
    Exchanges the authorization code for tokens, fetches user info,
    and returns a JWT + user profile.
    """
    code = request.args.get('code', '').strip()
    if not code:
        return jsonify({
            'error': 'Missing authorization code',
            'code': 'MISSING_CODE'
        }), 400

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        return jsonify({
            'error': 'Google OAuth is not configured',
            'code': 'OAUTH_NOT_CONFIGURED'
        }), 500

    try:
        # Exchange authorization code for tokens
        token_resp = requests.post(GOOGLE_TOKEN_URL, data={
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': GOOGLE_REDIRECT_URI,
        }, timeout=15)

        if token_resp.status_code != 200:
            return jsonify({
                'error': 'Failed to exchange authorization code',
                'code': 'TOKEN_EXCHANGE_FAILED',
                'detail': token_resp.text[:500]
            }), 400

        token_data = token_resp.json()
        access_token = token_data.get('access_token')

        if not access_token:
            return jsonify({
                'error': 'No access token in response',
                'code': 'NO_ACCESS_TOKEN'
            }), 400

        # Fetch user info from Google
        userinfo_resp = requests.get(
            GOOGLE_USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=15
        )

        if userinfo_resp.status_code != 200:
            return jsonify({
                'error': 'Failed to fetch user info',
                'code': 'USERINFO_FETCH_FAILED'
            }), 400

        userinfo = userinfo_resp.json()
        google_sub = userinfo.get('sub')
        email = userinfo.get('email')
        name = userinfo.get('name')
        picture = userinfo.get('picture')

        if not google_sub:
            return jsonify({
                'error': 'Invalid user info response',
                'code': 'INVALID_USERINFO'
            }), 400

        # Create or get user
        user_id, username, is_new = create_or_get_oauth_user(
            provider='google',
            oauth_id=google_sub,
            email=email,
            name=name,
            avatar_url=picture
        )

        # Generate JWT
        token = create_token(user_id, username, name=name)

        return jsonify({
            'message': 'Google sign-in successful',
            'token': token,
            'user': {
                'id': user_id,
                'username': username,
                'email': email,
                'name': name,
                'avatar_url': picture,
                'oauth_provider': 'google',
            },
            'is_new': is_new
        }), 200

    except requests.exceptions.RequestException as e:
        return jsonify({
            'error': f'Network error: {str(e)}',
            'code': 'NETWORK_ERROR'
        }), 500
    except Exception as e:
        return jsonify({
            'error': f'Google sign-in failed: {str(e)}',
            'code': 'OAUTH_FAILED'
        }), 500


# =============================================================================
#  Apple Sign-In
# =============================================================================

APPLE_CLIENT_ID = os.environ.get('APPLE_CLIENT_ID', '')
APPLE_TEAM_ID = os.environ.get('APPLE_TEAM_ID', '')
APPLE_KEY_ID = os.environ.get('APPLE_KEY_ID', '')
APPLE_PRIVATE_KEY_PATH = os.environ.get('APPLE_PRIVATE_KEY_PATH', '')

APPLE_JWKS_URL = 'https://appleid.apple.com/auth/keys'
APPLE_ISSUER = 'https://appleid.apple.com'

# Cache for Apple JWKS (simple in-memory, short TTL)
_apple_jwks_cache = {'keys': None, 'fetched_at': 0}
_APPLE_JWKS_TTL = 3600  # 1 hour


def _get_apple_jwks():
    """
    Fetch Apple's JWKS, with simple in-memory caching.
    Returns the 'keys' list from the JWKS endpoint.
    """
    now = time.time()
    if _apple_jwks_cache['keys'] is not None and (now - _apple_jwks_cache['fetched_at']) < _APPLE_JWKS_TTL:
        return _apple_jwks_cache['keys']

    resp = requests.get(APPLE_JWKS_URL, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f'Failed to fetch Apple JWKS: HTTP {resp.status_code}')

    jwks = resp.json()
    keys = jwks.get('keys', [])
    _apple_jwks_cache['keys'] = keys
    _apple_jwks_cache['fetched_at'] = now
    return keys


def _verify_apple_id_token(id_token):
    """
    Verify an Apple identity_token (JWT).
    1. Fetch Apple JWKS to get the public key matching the token's kid.
    2. Decode and verify the JWT signature, audience, and issuer.
    Returns the decoded payload dict.
    Raises ValueError with a message on failure.
    """
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    # Decode header (without verification) to get kid
    try:
        unverified_header = jwt.get_unverified_header(id_token)
    except Exception:
        raise ValueError('Invalid identity_token header')

    kid = unverified_header.get('kid')
    if not kid:
        raise ValueError('Missing kid in identity_token header')
    alg = unverified_header.get('alg', 'RS256')

    # Fetch Apple JWKS and find matching key
    keys = _get_apple_jwks()
    matching_key = None
    for key in keys:
        if key.get('kid') == kid:
            matching_key = key
            break

    if not matching_key:
        raise ValueError(f'No matching Apple JWK for kid={kid}')

    # Build RSA public key from JWK
    from jwt.algorithms import RSAAlgorithm
    try:
        public_key = RSAAlgorithm.from_jwk(json.dumps(matching_key))
    except Exception as e:
        raise ValueError(f'Failed to construct Apple public key: {e}')

    # Decode and verify the identity_token
    try:
        payload = jwt.decode(
            id_token,
            public_key,
            algorithms=[alg],
            audience=APPLE_CLIENT_ID,
            issuer=APPLE_ISSUER,
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError('Apple identity_token has expired')
    except jwt.InvalidAudienceError:
        raise ValueError(f'Invalid aud in identity_token, expected {APPLE_CLIENT_ID}')
    except jwt.InvalidIssuerError:
        raise ValueError(f'Invalid iss in identity_token, expected {APPLE_ISSUER}')
    except jwt.InvalidTokenError as e:
        raise ValueError(f'Apple identity_token verification failed: {e}')


def apple_auth_callback():
    """
    POST /api/auth/apple/callback
    Receives JSON body: {
      identity_token: string,
      name?: {firstName, lastName},
      authorization_code?: string,
      user?: string
    }
    Validates the identity_token against Apple's JWKS and creates/logs in the user.
    """
    if not APPLE_CLIENT_ID:
        return jsonify({
            'error': 'Apple Sign-In is not configured',
            'code': 'OAUTH_NOT_CONFIGURED'
        }), 500

    data = request.get_json(silent=True) or {}
    identity_token = (data.get('identity_token') or '').strip()

    if not identity_token:
        return jsonify({
            'error': 'Missing identity_token',
            'code': 'MISSING_TOKEN'
        }), 400

    try:
        # Verify the identity_token signature and claims
        id_payload = _verify_apple_id_token(identity_token)

        apple_sub = id_payload.get('sub')
        email = id_payload.get('email')

        if not apple_sub:
            return jsonify({
                'error': 'Missing sub claim in identity_token',
                'code': 'INVALID_TOKEN'
            }), 400

        # Extract name from request body (Apple only sends name on first sign-in)
        name = None
        apple_name = data.get('name')
        if apple_name and isinstance(apple_name, dict):
            first = (apple_name.get('firstName') or '').strip()
            last = (apple_name.get('lastName') or '').strip()
            if first or last:
                name = f'{first} {last}'.strip()

        # Create or get user
        user_id, username, is_new = create_or_get_oauth_user(
            provider='apple',
            oauth_id=apple_sub,
            email=email,
            name=name,
            avatar_url=None
        )

        # Generate JWT
        token = create_token(user_id, username, name=name)

        return jsonify({
            'message': 'Apple sign-in successful',
            'token': token,
            'user': {
                'id': user_id,
                'username': username,
                'email': email,
                'name': name,
                'avatar_url': None,
                'oauth_provider': 'apple',
            },
            'is_new': is_new
        }), 200

    except ValueError as e:
        return jsonify({
            'error': str(e),
            'code': 'TOKEN_VERIFICATION_FAILED'
        }), 401
    except requests.exceptions.RequestException as e:
        return jsonify({
            'error': f'Network error fetching Apple JWKS: {str(e)}',
            'code': 'NETWORK_ERROR'
        }), 500
    except Exception as e:
        return jsonify({
            'error': f'Apple sign-in failed: {str(e)}',
            'code': 'OAUTH_FAILED'
        }), 500


# =============================================================================
#  WeChat OAuth (微信网页授权)
# =============================================================================

WEIXIN_APPID = os.environ.get('WEIXIN_APPID', '')
WEIXIN_APPSECRET = os.environ.get('WEIXIN_APPSECRET', '')
WEIXIN_REDIRECT_URI = os.environ.get('WEIXIN_REDIRECT_URI', '')

# 微信 API 走代理
WEIXIN_PROXY = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY') or 'http://172.23.80.1:7890'

WEIXIN_AUTH_URL = 'https://open.weixin.qq.com/connect/oauth2/authorize'
WEIXIN_TOKEN_URL = 'https://api.weixin.qq.com/sns/oauth2/access_token'
WEIXIN_USERINFO_URL = 'https://api.weixin.qq.com/sns/userinfo'

_wechat_session = None


def _get_wechat_session():
    """获取带代理的 requests Session（微信 API 需走代理）"""
    global _wechat_session
    if _wechat_session is None:
        _wechat_session = requests.Session()
        _wechat_session.proxies = {'http': WEIXIN_PROXY, 'https': WEIXIN_PROXY}
        _wechat_session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; Parro/1.0)',
        })
    return _wechat_session


def wechat_auth_url():
    """
    GET /api/auth/wechat/url
    返回微信网页授权 URL，前端跳转到该地址让用户授权。
    """
    if not WEIXIN_APPID or not WEIXIN_REDIRECT_URI:
        return jsonify({
            'error': '微信登录未配置',
            'code': 'OAUTH_NOT_CONFIGURED'
        }), 500

    params = {
        'appid': WEIXIN_APPID,
        'redirect_uri': WEIXIN_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'snsapi_userinfo',
    }
    query = urlencode(params)
    auth_url = f'{WEIXIN_AUTH_URL}?{query}#wechat_redirect'

    return jsonify({
        'url': auth_url,
        'provider': 'wechat'
    }), 200


def wechat_auth_callback():
    """
    GET /api/auth/wechat/callback?code=xxx&state=xxx
    处理微信授权回调：code 换 access_token → 获取用户信息 → 创建/查找用户 → 返回 JWT。
    """
    code = request.args.get('code', '').strip()
    if not code:
        return jsonify({
            'error': '缺少授权 code',
            'code': 'MISSING_CODE'
        }), 400

    if not WEIXIN_APPID or not WEIXIN_APPSECRET:
        return jsonify({
            'error': '微信登录未配置',
            'code': 'OAUTH_NOT_CONFIGURED'
        }), 500

    try:
        sess = _get_wechat_session()

        # Step 1: code 换 access_token
        token_resp = sess.get(WEIXIN_TOKEN_URL, params={
            'appid': WEIXIN_APPID,
            'secret': WEIXIN_APPSECRET,
            'code': code,
            'grant_type': 'authorization_code',
        }, timeout=15)

        if token_resp.status_code != 200:
            return jsonify({
                'error': '微信 access_token 请求失败',
                'code': 'TOKEN_EXCHANGE_FAILED',
                'detail': token_resp.text[:500]
            }), 400

        token_data = token_resp.json()

        # 微信错误码检查
        if token_data.get('errcode'):
            return jsonify({
                'error': f"微信返回错误: {token_data.get('errmsg', 'unknown')}",
                'code': 'WECHAT_ERROR',
                'errcode': token_data.get('errcode')
            }), 400

        access_token = token_data.get('access_token')
        openid = token_data.get('openid')

        if not access_token or not openid:
            return jsonify({
                'error': '微信返回数据不完整',
                'code': 'NO_ACCESS_TOKEN'
            }), 400

        # Step 2: 获取用户信息
        userinfo_resp = sess.get(WEIXIN_USERINFO_URL, params={
            'access_token': access_token,
            'openid': openid,
            'lang': 'zh_CN',
        }, timeout=15)

        if userinfo_resp.status_code != 200:
            return jsonify({
                'error': '获取微信用户信息失败',
                'code': 'USERINFO_FETCH_FAILED'
            }), 400

        userinfo = userinfo_resp.json()

        if userinfo.get('errcode'):
            return jsonify({
                'error': f"获取微信用户信息错误: {userinfo.get('errmsg', 'unknown')}",
                'code': 'WECHAT_ERROR',
                'errcode': userinfo.get('errcode')
            }), 400

        nickname = userinfo.get('nickname', '')
        headimgurl = userinfo.get('headimgurl', '')
        unionid = userinfo.get('unionid') or None

        # oauth_id: 优先用 unionid（跨应用唯一），否则用 openid
        oauth_id = unionid or openid

        # name: 过滤掉微信昵称中可能的特殊字符
        import re
        safe_name = re.sub(r'[^\w\u4e00-\u9fff\s\-_]', '', nickname).strip() if nickname else None
        if not safe_name:
            safe_name = None

        # Create or get user
        user_id, username, is_new = create_or_get_oauth_user(
            provider='wechat',
            oauth_id=oauth_id,
            email=None,  # 微信不返回邮箱
            name=safe_name,
            avatar_url=headimgurl or None
        )

        # Generate JWT
        token = create_token(user_id, username, name=safe_name)

        return jsonify({
            'message': '微信登录成功',
            'token': token,
            'user': {
                'id': user_id,
                'username': username,
                'email': None,
                'name': safe_name,
                'avatar_url': headimgurl or None,
                'oauth_provider': 'wechat',
            },
            'is_new': is_new
        }), 200

    except requests.exceptions.RequestException as e:
        return jsonify({
            'error': f'网络错误: {str(e)}',
            'code': 'NETWORK_ERROR'
        }), 500
    except Exception as e:
        return jsonify({
            'error': f'微信登录失败: {str(e)}',
            'code': 'OAUTH_FAILED'
        }), 500


# =============================================================================
#  Alipay OAuth (支付宝第三方登录)
# =============================================================================

ALIPAY_APP_ID = os.environ.get('ALIPAY_APP_ID', '')
ALIPAY_PRIVATE_KEY = os.environ.get('ALIPAY_PRIVATE_KEY', '')
ALIPAY_PUBLIC_KEY = os.environ.get('ALIPAY_PUBLIC_KEY', '')
ALIPAY_REDIRECT_URI = os.environ.get('ALIPAY_REDIRECT_URI', '')

ALIPAY_AUTH_URL = 'https://openauth.alipay.com/oauth2/publicAppAuthorize.htm'
ALIPAY_GATEWAY_URL = 'https://openapi.alipay.com/gateway.do'


def _alipay_sign(params: dict, private_key_pem: str) -> str:
    """
    支付宝 RSA2-SHA256 签名。
    1. 按字母序排列参数
    2. 拼接 key=value 用 & 连接
    3. 使用 SHA256-RSA 签名
    4. Base64 编码
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend

    # Step 1 & 2: 排序拼接
    sorted_keys = sorted(params.keys())
    sign_str = '&'.join(f'{k}={params[k]}' for k in sorted_keys if params[k] is not None)

    # Step 3: 加载私钥并签名
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode('utf-8'),
        password=None,
        backend=default_backend()
    )

    signature = private_key.sign(
        sign_str.encode('utf-8'),
        padding.PKCS1v15(),
        hashes.SHA256()
    )

    # Step 4: Base64 编码
    return base64.b64encode(signature).decode('utf-8')


def _alipay_verify_sign(sign_str: str, signature: str, public_key_pem: str) -> bool:
    """验证支付宝异步通知签名（预留，OAuth 流程中不一定需要）"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend

    try:
        public_key = serialization.load_pem_public_key(
            public_key_pem.encode('utf-8'),
            backend=default_backend()
        )
        public_key.verify(
            base64.b64decode(signature),
            sign_str.encode('utf-8'),
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        return True
    except Exception:
        return False


def _alipay_call(method: str, biz_content: dict) -> dict:
    """
    调用支付宝 API（openapi.alipay.com/gateway.do）。
    不走代理（国内接口）。
    """
    import time as _time

    params = {
        'app_id': ALIPAY_APP_ID,
        'method': method,
        'charset': 'utf-8',
        'sign_type': 'RSA2',
        'timestamp': _time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime()),
        'version': '1.0',
        'biz_content': json.dumps(biz_content, ensure_ascii=False),
    }
    if ALIPAY_REDIRECT_URI:
        params['return_url'] = ALIPAY_REDIRECT_URI

    # 签名
    params['sign'] = _alipay_sign(params, ALIPAY_PRIVATE_KEY)

    # 发送请求（支付宝国内接口，不走代理）
    resp = requests.post(ALIPAY_GATEWAY_URL, data=params, timeout=15)
    resp.encoding = 'utf-8'

    return resp.json()


def alipay_auth_url():
    """
    GET /api/auth/alipay/url
    返回支付宝授权 URL，前端跳转到该地址让用户授权。
    """
    if not ALIPAY_APP_ID or not ALIPAY_REDIRECT_URI:
        return jsonify({
            'error': '支付宝登录未配置',
            'code': 'OAUTH_NOT_CONFIGURED'
        }), 500

    params = {
        'app_id': ALIPAY_APP_ID,
        'redirect_uri': ALIPAY_REDIRECT_URI,
        'scope': 'auth_user',
    }
    query = urlencode(params)
    auth_url = f'{ALIPAY_AUTH_URL}?{query}'

    return jsonify({
        'url': auth_url,
        'provider': 'alipay'
    }), 200


def alipay_auth_callback():
    """
    GET /api/auth/alipay/callback?auth_code=xxx
    处理支付宝授权回调：auth_code 换 access_token → 获取用户信息 → 创建/查找用户 → 返回 JWT。
    """
    auth_code = request.args.get('auth_code', '').strip()
    if not auth_code:
        return jsonify({
            'error': '缺少授权 auth_code',
            'code': 'MISSING_CODE'
        }), 400

    if not ALIPAY_APP_ID or not ALIPAY_PRIVATE_KEY:
        return jsonify({
            'error': '支付宝登录未配置',
            'code': 'OAUTH_NOT_CONFIGURED'
        }), 500

    try:
        # Step 1: auth_code 换 access_token
        token_result = _alipay_call('alipay.system.oauth.token', {
            'grant_type': 'authorization_code',
            'code': auth_code,
        })

        token_response = token_result.get('alipay_system_oauth_token_response', {})
        if not token_response:
            return jsonify({
                'error': '支付宝 token 请求失败',
                'code': 'TOKEN_EXCHANGE_FAILED',
                'detail': json.dumps(token_result, ensure_ascii=False)[:500]
            }), 400

        access_token = token_response.get('access_token')
        user_id_alipay = token_response.get('user_id')

        if not access_token or not user_id_alipay:
            # 检查是否有错误
            error_msg = token_response.get('sub_msg') or token_response.get('msg') or '支付宝返回数据不完整'
            return jsonify({
                'error': error_msg,
                'code': 'NO_ACCESS_TOKEN'
            }), 400

        # Step 2: 获取用户信息（需要 auth_token 作为额外参数）
        import time as _time

        biz_content = json.dumps({}, ensure_ascii=False)
        params = {
            'app_id': ALIPAY_APP_ID,
            'method': 'alipay.user.info.share',
            'charset': 'utf-8',
            'sign_type': 'RSA2',
            'timestamp': _time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime()),
            'version': '1.0',
            'biz_content': biz_content,
            'auth_token': access_token,
        }
        params['sign'] = _alipay_sign(params, ALIPAY_PRIVATE_KEY)

        resp = requests.post(ALIPAY_GATEWAY_URL, data=params, timeout=15)
        resp.encoding = 'utf-8'
        info_result = resp.json()

        info_response = info_result.get('alipay_user_info_share_response', {})
        if not info_response:
            return jsonify({
                'error': '获取支付宝用户信息失败',
                'code': 'USERINFO_FETCH_FAILED',
                'detail': json.dumps(info_result, ensure_ascii=False)[:500]
            }), 400

        # 检查错误码
        code = info_response.get('code', '')
        if code != '10000':
            return jsonify({
                'error': f"获取支付宝用户信息错误: {info_response.get('sub_msg', info_response.get('msg', 'unknown'))}",
                'code': 'ALIPAY_ERROR',
                'alipay_code': code
            }), 400

        nick_name = info_response.get('nick_name', '')
        avatar = info_response.get('avatar', '')

        # Create or get user
        user_id, username, is_new = create_or_get_oauth_user(
            provider='alipay',
            oauth_id=user_id_alipay,
            email=None,  # 支付宝 user.info.share 不返回邮箱
            name=nick_name or None,
            avatar_url=avatar or None
        )

        # Generate JWT
        token = create_token(user_id, username, name=nick_name or None)

        return jsonify({
            'message': '支付宝登录成功',
            'token': token,
            'user': {
                'id': user_id,
                'username': username,
                'email': None,
                'name': nick_name or None,
                'avatar_url': avatar or None,
                'oauth_provider': 'alipay',
            },
            'is_new': is_new
        }), 200

    except requests.exceptions.RequestException as e:
        return jsonify({
            'error': f'网络错误: {str(e)}',
            'code': 'NETWORK_ERROR'
        }), 500
    except Exception as e:
        return jsonify({
            'error': f'支付宝登录失败: {str(e)}',
            'code': 'OAUTH_FAILED'
        }), 500
