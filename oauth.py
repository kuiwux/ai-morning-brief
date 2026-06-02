"""
Parro OAuth module — Google & Apple Sign-In
Handles OAuth login flows and user lookup/creation.
"""

import os
import time
import json
import requests

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
