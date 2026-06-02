#!/usr/bin/env python3
"""
Parro（硅谷AI晨报）— Flask 后端服务器
端口: 8899
功能:
  - 用户系统：注册/登录/Session/偏好设置
  - 静态文件服务（index.html, manifest.json, sw.js, icons, push-notification.js）
  - VAPID 公钥端点 GET /api/vapid-public-key
  - 推送订阅/取消订阅
  - 语音列表 / TTS
  - 今日资讯 API
  - 正确的 Content-Type 和 CORS 头
"""

import json
import os
import sys
import hashlib
import asyncio
import concurrent.futures

from flask import Flask, request, jsonify, send_from_directory, g, Response
from database import init_db, create_demo_account, get_db
from auth import login_required, hash_password, verify_password, create_token, decode_token, blacklist_token
from oauth import google_auth_url, google_auth_callback, apple_auth_callback

# ===== 配置 =====
PORT = int(os.environ.get('PORT', 8899))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 确保 data 目录在 sys.path 中
sys.path.insert(0, BASE_DIR)

# ===== VAPID Keys =====
VAPID_PUBLIC_KEY = "BDJqNKhTZQHt6GbKpMxPqS3vW8yL2nR4xYcF7aB1dE5gH9iJ0kLmN3oP6qR5sT7uV8wX1yZ2aB3cD4eF5gH6i"

# 内存中的推送订阅列表
push_subscriptions = []

# ===== Flask 应用初始化 =====
app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')

# ===== CORS 支持 =====
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response


# ===== 数据库初始化 =====
# ── 语音 DB 辅助函数（定义在启动块之前） ──
def _init_voice_db_early():
    """初始化语音数据库"""
    try:
        from voice_db import init_voice_db
        init_voice_db()
        print('[Voice] 语音数据库已初始化')
    except Exception as e:
        print(f'[Voice] 语音数据库初始化失败（非致命）: {e}')

with app.app_context():
    init_db()
    create_demo_account()
    _init_voice_db_early()


# =====================================================================
#  静态文件服务
# =====================================================================

@app.route('/')
def serve_index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    """服务所有静态文件（js, css, png, json, ico 等）"""
    # 排除 API 路由
    if filename.startswith('api/'):
        return jsonify({'error': 'Not found'}), 404
    return send_from_directory(BASE_DIR, filename)


# =====================================================================
#  认证 API
# =====================================================================

@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    """
    用户注册
    POST /api/auth/register
    Body: {"username": "user@example.com", "password": "xxx"}
    """
    data = request.get_json(silent=True) or {}

    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    # 验证输入
    if not username or not password:
        return jsonify({
            'error': '用户名和密码不能为空',
            'code': 'INVALID_INPUT'
        }), 400

    if len(username) < 3:
        return jsonify({
            'error': '用户名至少需要 3 个字符',
            'code': 'USERNAME_TOO_SHORT'
        }), 400

    if len(password) < 6:
        return jsonify({
            'error': '密码至少需要 6 个字符',
            'code': 'PASSWORD_TOO_SHORT'
        }), 400

    # 检查用户名是否已存在
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()

        if existing:
            return jsonify({
                'error': '该用户名已被注册',
                'code': 'USERNAME_EXISTS'
            }), 409

        # 创建用户
        password_hash = hash_password(password)
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash)
        )
        user_id = cursor.lastrowid

        # 创建默认偏好设置
        conn.execute(
            "INSERT INTO user_preferences (user_id) VALUES (?)",
            (user_id,)
        )

        conn.commit()

        # 生成 token
        token = create_token(user_id, username)

        return jsonify({
            'message': '注册成功',
            'token': token,
            'user': {
                'id': user_id,
                'username': username
            }
        }), 201

    except Exception as e:
        conn.rollback()
        return jsonify({
            'error': f'注册失败: {str(e)}',
            'code': 'REGISTER_FAILED'
        }), 500
    finally:
        conn.close()


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """
    用户登录
    POST /api/auth/login
    Body: {"username": "demo@parro.app", "password": "ParroDemo2026!"}
    """
    data = request.get_json(silent=True) or {}

    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    if not username or not password:
        return jsonify({
            'error': '用户名和密码不能为空',
            'code': 'INVALID_INPUT'
        }), 400

    conn = get_db()
    try:
        user = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,)
        ).fetchone()

        if not user:
            return jsonify({
                'error': '用户名或密码错误',
                'code': 'INVALID_CREDENTIALS'
            }), 401

        if not verify_password(password, user['password_hash']):
            return jsonify({
                'error': '用户名或密码错误',
                'code': 'INVALID_CREDENTIALS'
            }), 401

        # 生成 token
        token = create_token(user['id'], user['username'])

        return jsonify({
            'message': '登录成功',
            'token': token,
            'user': {
                'id': user['id'],
                'username': user['username']
            }
        }), 200

    except Exception as e:
        return jsonify({
            'error': f'登录失败: {str(e)}',
            'code': 'LOGIN_FAILED'
        }), 500
    finally:
        conn.close()


@app.route('/api/auth/logout', methods=['POST'])
@login_required
def auth_logout():
    """
    用户登出（将当前 token 加入黑名单）
    POST /api/auth/logout
    Header: Authorization: Bearer <token>
    """
    token = g.current_token
    blacklist_token(token)
    return jsonify({'message': '登出成功'}), 200


@app.route('/api/auth/me', methods=['GET'])
@login_required
def auth_me():
    """
    获取当前登录用户信息
    GET /api/auth/me
    Header: Authorization: Bearer <token>
    """
    conn = get_db()
    try:
        user = conn.execute(
            "SELECT id, username, created_at, is_demo FROM users WHERE id = ?",
            (g.current_user_id,)
        ).fetchone()

        if not user:
            return jsonify({'error': '用户不存在'}), 404

        return jsonify({
            'user': {
                'id': user['id'],
                'username': user['username'],
                'created_at': user['created_at'],
                'is_demo': bool(user['is_demo'])
            }
        }), 200
    finally:
        conn.close()


# OAuth routes — registered via add_url_rule for clean namespacing
app.add_url_rule('/api/auth/google/url', 'google_auth_url', google_auth_url, methods=['GET'])
app.add_url_rule('/api/auth/google/callback', 'google_auth_callback', google_auth_callback, methods=['GET'])
app.add_url_rule('/api/auth/apple/callback', 'apple_auth_callback', apple_auth_callback, methods=['POST'])


# =====================================================================
#  用户偏好设置 API
# =====================================================================

@app.route('/api/user/preferences', methods=['GET'])
@login_required
def get_preferences():
    """
    获取当前用户的偏好设置
    GET /api/user/preferences
    Header: Authorization: Bearer <token>
    """
    conn = get_db()
    try:
        prefs = conn.execute(
            "SELECT * FROM user_preferences WHERE user_id = ?",
            (g.current_user_id,)
        ).fetchone()

        if not prefs:
            # 如果不存在则创建默认偏好
            conn.execute(
                "INSERT INTO user_preferences (user_id) VALUES (?)",
                (g.current_user_id,)
            )
            conn.commit()
            prefs = conn.execute(
                "SELECT * FROM user_preferences WHERE user_id = ?",
                (g.current_user_id,)
            ).fetchone()

        return jsonify({
            'preferences': {
                'topics': json.loads(prefs['topics']),
                'language': prefs['language'],
                'push_frequency': prefs['push_frequency'],
                'push_enabled': bool(prefs['push_enabled']),
                'voice_id': prefs['voice_id'],
                'voice_speed': prefs['voice_speed'],
                'updated_at': prefs['updated_at']
            }
        }), 200
    except Exception as e:
        return jsonify({'error': f'获取偏好失败: {str(e)}'}), 500
    finally:
        conn.close()


@app.route('/api/user/preferences', methods=['PUT'])
@login_required
def update_preferences():
    """
    更新用户偏好设置
    PUT /api/user/preferences
    Header: Authorization: Bearer <token>
    Body: {"topics": ["AI","科技"], "language": "zh", "push_frequency": "daily", ...}
    """
    data = request.get_json(silent=True) or {}

    conn = get_db()
    try:
        # 检查是否已有偏好记录
        existing = conn.execute(
            "SELECT id FROM user_preferences WHERE user_id = ?",
            (g.current_user_id,)
        ).fetchone()

        if not existing:
            conn.execute(
                "INSERT INTO user_preferences (user_id) VALUES (?)",
                (g.current_user_id,)
            )

        # 构建更新字段
        updates = []
        params = []

        if 'topics' in data:
            topics = data['topics']
            if isinstance(topics, list):
                updates.append("topics = ?")
                params.append(json.dumps(topics, ensure_ascii=False))

        if 'language' in data:
            lang = data['language']
            if lang in ('zh', 'en'):
                updates.append("language = ?")
                params.append(lang)

        if 'push_frequency' in data:
            freq = data['push_frequency']
            if freq in ('daily', 'weekly', 'realtime', 'off'):
                updates.append("push_frequency = ?")
                params.append(freq)

        if 'push_enabled' in data:
            updates.append("push_enabled = ?")
            params.append(1 if data['push_enabled'] else 0)

        if 'voice_id' in data:
            updates.append("voice_id = ?")
            params.append(data['voice_id'])

        if 'voice_speed' in data:
            speed = float(data['voice_speed'])
            speed = max(0.5, min(2.0, speed))  # 限制在 0.5 ~ 2.0
            updates.append("voice_speed = ?")
            params.append(speed)

        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(g.current_user_id)
            conn.execute(
                f"UPDATE user_preferences SET {', '.join(updates)} WHERE user_id = ?",
                params
            )
            conn.commit()

        # 返回更新后的偏好
        prefs = conn.execute(
            "SELECT * FROM user_preferences WHERE user_id = ?",
            (g.current_user_id,)
        ).fetchone()

        return jsonify({
            'message': '偏好设置已更新',
            'preferences': {
                'topics': json.loads(prefs['topics']),
                'language': prefs['language'],
                'push_frequency': prefs['push_frequency'],
                'push_enabled': bool(prefs['push_enabled']),
                'voice_id': prefs['voice_id'],
                'voice_speed': prefs['voice_speed'],
                'updated_at': prefs['updated_at']
            }
        }), 200

    except Exception as e:
        conn.rollback()
        return jsonify({'error': f'更新偏好失败: {str(e)}'}), 500
    finally:
        conn.close()


# =====================================================================
#  VAPID / 推送 API（保持原有功能）
# =====================================================================

@app.route('/api/vapid-public-key', methods=['GET'])
def get_vapid_key():
    """获取 VAPID 公钥"""
    return jsonify({'publicKey': VAPID_PUBLIC_KEY})


@app.route('/api/push/subscribe', methods=['POST'])
def push_subscribe():
    """订阅推送"""
    data = request.get_json(silent=True) or {}
    subscription = {
        'endpoint': data.get('endpoint', ''),
        'keys': data.get('keys', {}),
        'userAgent': data.get('userAgent', ''),
        'timestamp': data.get('timestamp', '')
    }

    global push_subscriptions
    push_subscriptions = [
        s for s in push_subscriptions
        if s.get('endpoint') != subscription['endpoint']
    ]
    push_subscriptions.append(subscription)

    print(f'[Push] 新订阅已添加。总数: {len(push_subscriptions)}')
    return jsonify({'status': 'ok', 'message': 'Subscribed successfully'})


@app.route('/api/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    """取消推送订阅"""
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint', '')
    global push_subscriptions
    push_subscriptions[:] = [
        s for s in push_subscriptions
        if s.get('endpoint') != endpoint
    ]
    print(f'[Push] 订阅已取消。总数: {len(push_subscriptions)}')
    return jsonify({'status': 'ok', 'message': 'Unsubscribed successfully'})


@app.route('/api/push/subscriptions', methods=['GET'])
def list_subscriptions():
    """列出所有订阅（调试用）"""
    return jsonify({
        'count': len(push_subscriptions),
        'subscriptions': push_subscriptions
    })


# =====================================================================
#  语音 & TTS API
# =====================================================================

# ── TTS 缓存目录 ─────────────────────────────────────────────────────────
TTS_CACHE_DIR = os.path.join(BASE_DIR, 'data', 'tts_cache')
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

# ── 延迟加载 voice_service & voice_db ─────────────────────────────────────
_voice_service = None
_voice_import_error = None


def _get_voice_service():
    """延迟加载 VoiceService 单例"""
    global _voice_service, _voice_import_error
    if _voice_service is not None:
        return _voice_service
    try:
        from voice_service import voice_service as vs, EDGE_VOICE_MAP
        _voice_service = vs
        return vs
    except Exception as e:
        _voice_import_error = str(e)
        print(f'[Voice] 加载 voice_service 失败: {e}')
        return None


def _get_edge_voice_map():
    """获取 Edge TTS 语音映射"""
    try:
        from voice_service import EDGE_VOICE_MAP
        return EDGE_VOICE_MAP
    except Exception:
        return {}


def _run_async(coro, timeout: float = 60.0):
    """在 Flask 同步上下文中运行异步协程，返回结果"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result(timeout=timeout)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


@app.route('/api/v2/voice/list', methods=['GET'])
def voice_list():
    """获取可用语音列表（8 预设：中英各 4）"""
    edge_map = _get_edge_voice_map()
    voices = []
    for voice_id, info in edge_map.items():
        voices.append({
            'voice_id': voice_id,
            'name': info.get('name', voice_id),
            'display_name': info.get('name', voice_id),
            'gender': 'female' if 'female' in info.get('name', '').lower() or 'xiaoxiao' in voice_id or 'aria' in voice_id or 'jenny' in voice_id else 'male',
            'style': info.get('style', ''),
            'lang': info.get('lang', 'zh'),
            'avatar': info.get('emoji', '🎤'),
            'avatar_class': f"vca-{info.get('lang', 'zh')}",
            'sample_text': 'Welcome to Silicon Valley AI Morning Brief. I am your AI news anchor, bringing you the latest updates every day.' if info.get('lang') == 'en' else '欢迎使用硅谷AI晨报语音播报，每天五分钟，掌握AI圈最新动态。'
        })
    return jsonify({'voices': voices})


@app.route('/api/v2/voice/tts', methods=['POST'])
def voice_tts():
    """生成 TTS 语音（使用 edge-tts 真实 TTS，带缓存）"""
    data = request.get_json(silent=True) or {}
    text = (data.get('text', '') or '').strip()
    voice_id = (data.get('voice_id', '') or '').strip()
    speed = float(data.get('speed', 1.0))

    if not text:
        return jsonify({'error': '缺少 text 参数'}), 400

    if not voice_id:
        voice_id = 'edge_yunxi'

    # 检查预设语音映射
    edge_map = _get_edge_voice_map()
    if voice_id not in edge_map:
        return jsonify({'error': f'不支持的语音 ID: {voice_id}'}), 400

    edge_voice = edge_map[voice_id]['voice']

    # 计算缓存键（text + voice_id + speed 的 SHA256）
    cache_key = hashlib.sha256(
        f"{text}|{voice_id}|{speed:.2f}".encode('utf-8')
    ).hexdigest()
    cache_path = os.path.join(TTS_CACHE_DIR, f"{cache_key}.mp3")

    # 命中缓存直接返回
    if os.path.exists(cache_path):
        print(f'[TTS] 缓存命中: {voice_id}, text_len={len(text)}')
        with open(cache_path, 'rb') as f:
            audio_data = f.read()
        return Response(audio_data, mimetype='audio/mpeg',
                        headers={'X-Voice-Source': 'edge-cache',
                                 'X-Voice-Id': voice_id})

    print(f'[TTS] 生成语音: voice={voice_id}({edge_voice}), speed={speed}, text_len={len(text)}')

    # 计算 edge-tts rate 参数
    rate_percent = int((speed - 1.0) * 100)
    rate_str = f"{'+' if rate_percent >= 0 else ''}{rate_percent}%"

    async def _generate():
        import edge_tts
        # 使用代理连接 Microsoft TTS 服务
        proxy_url = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY') or 'http://172.23.80.1:7890'
        communicate = edge_tts.Communicate(
            text=text, voice=edge_voice, rate=rate_str,
            proxy=proxy_url,
        )
        # 使用 save() 直接保存到文件，更稳定
        tmp_path = cache_path + '.tmp'
        await communicate.save(tmp_path)
        # 原子移动
        os.rename(tmp_path, cache_path)
        with open(cache_path, 'rb') as f:
            return f.read()

    try:
        audio_data = _run_async(_generate(), timeout=60.0)
        print(f'[TTS] edge-tts 生成 {len(audio_data)} 字节, 缓存: {cache_path}')
        return Response(audio_data, mimetype='audio/mpeg',
                        headers={'X-Voice-Source': 'edge',
                                 'X-Voice-Id': voice_id})
    except asyncio.TimeoutError:
        return jsonify({'error': 'TTS 生成超时（60秒）'}), 504
    except Exception as e:
        print(f'[TTS] edge-tts 失败: {e}')
        return jsonify({'error': f'TTS 生成失败: {str(e)}'}), 500


# ── 语音克隆 API ─────────────────────────────────────────────────────────

@app.route('/api/v2/voice/clone', methods=['POST'])
def voice_clone():
    """
    上传音频文件，克隆语音（需要 FISH_AUDIO_API_KEY）。
    multipart/form-data:
        audio (file): 音频文件
        name  (str) : 语音名称
        description (str, optional): 语音描述
    返回: {id, name, status, fish_voice_id, demo_mode?}
    """
    vs = _get_voice_service()
    if vs is None:
        return jsonify({'error': f'voice_service 加载失败: {_voice_import_error}'}), 500

    if vs.is_demo_mode():
        return jsonify({
            'error': '语音克隆需要设置 FISH_AUDIO_API_KEY 环境变量',
            'hint': '请在环境变量中配置 FISH_AUDIO_API_KEY=your_key 后重启服务',
            'demo_mode': True,
        }), 400

    try:
        import uuid as _uuid_mod
        if 'audio' not in request.files:
            return jsonify({'error': '缺少 audio 文件'}), 400

        audio_file = request.files['audio']
        name = (request.form.get('name', '') or '').strip()

        if not name:
            return jsonify({'error': '缺少 name 参数'}), 400

        if audio_file.filename == '':
            return jsonify({'error': '文件名为空'}), 400

        # 检查文件大小
        audio_file.seek(0, os.SEEK_END)
        file_size = audio_file.tell()
        audio_file.seek(0)

        if file_size > 10 * 1024 * 1024:
            return jsonify({'error': f'文件过大（{file_size} bytes），最大支持 10MB'}), 400

        # 检查文件扩展名
        ext = os.path.splitext(audio_file.filename)[1].lower().lstrip('.')
        allowed_exts = {'mp3', 'wav', 'm4a', 'mp4', 'mov', 'flac', 'ogg', 'aac', 'wma'}
        if ext not in allowed_exts:
            return jsonify({'error': f'不支持的文件格式: .{ext}，支持: {", ".join(sorted(allowed_exts))}'}), 400

        # 保存到本地上传目录
        upload_dir = os.path.join(BASE_DIR, 'voice_uploads')
        os.makedirs(upload_dir, exist_ok=True)
        local_id = _uuid_mod.uuid4().hex[:12]
        local_filename = f"{local_id}.{ext}"
        local_path = os.path.join(upload_dir, local_filename)
        audio_file.save(local_path)

        print(f'[Voice] 音频已保存: {local_path} ({file_size} bytes)')

        # 调用 Fish Audio 克隆
        description = request.form.get('description', '')
        result = _run_async(vs.clone_voice(local_path, name, description), timeout=120.0)

        if 'error' in result:
            return jsonify(result), 500

        # 写入数据库
        try:
            from voice_db import insert_cloned_voice
            insert_cloned_voice(
                voice_id=local_id,
                name=name,
                fish_voice_id=result.get('voice_id', ''),
                source_audio_path=local_path,
                status=result.get('status', 'training'),
                preview_url=result.get('preview_url', ''),
            )
        except Exception as e:
            print(f'[Voice] 写入数据库失败（非致命）: {e}')

        response_data = {
            'id': local_id,
            'name': name,
            'status': result.get('status', 'training'),
            'preview_url': result.get('preview_url', ''),
            'fish_voice_id': result.get('voice_id', ''),
        }

        if result.get('demo_mode'):
            response_data['demo_mode'] = True
            response_data['message'] = result.get('message', '')

        return jsonify(response_data)

    except Exception as e:
        print(f'[Voice] /api/v2/voice/clone 错误: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/v2/voice/clones', methods=['GET'])
def voice_clones_list():
    """列出已克隆的语音"""
    try:
        from voice_db import get_cloned_voices
        cloned = get_cloned_voices()
        return jsonify({'clones': cloned, 'total': len(cloned)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/v2/voice/clone/<clone_id>', methods=['DELETE'])
def voice_clone_delete(clone_id: str):
    """删除克隆语音"""
    vs = _get_voice_service()
    try:
        from voice_db import get_cloned_voice, delete_cloned_voice

        # 检查是否存在
        cloned = get_cloned_voice(clone_id)
        if not cloned:
            return jsonify({'error': f'克隆语音 {clone_id} 不存在'}), 404

        # 删除本地文件
        source_path = cloned.get('source_audio_path', '')
        if source_path and os.path.isfile(source_path):
            try:
                os.remove(source_path)
                print(f'[Voice] 已删除音频文件: {source_path}')
            except OSError as e:
                print(f'[Voice] 删除文件失败: {e}')

        # 删除 Fish Audio 上的语音（非阻塞）
        fish_voice_id = cloned.get('fish_voice_id', '')
        if fish_voice_id and vs and not vs.is_demo_mode():
            try:
                _run_async(vs.delete_voice(fish_voice_id), timeout=30.0)
            except Exception:
                pass

        # 删除数据库记录
        delete_cloned_voice(clone_id)

        return jsonify({'status': 'ok', 'deleted': clone_id})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =====================================================================
#  资讯 API
# =====================================================================

# 硬编码降级数据（当 output.json 不可用时使用）
_FALLBACK_ARTICLES = [
    {'id': 'fallback_1', 'title': 'OpenAI 宣布完成新一轮融资', 'summary': '估值3000亿美元，由Thrive Capital领投', 'category': '公司动态', 'tags': ['OpenAI', '融资'], 'source_type': 'hn', 'language': 'zh'},
    {'id': 'fallback_2', 'title': 'DeepSeek 开源最新模型 V4', 'summary': '性能对标 Claude 4，训练成本仅为600万美元', 'category': '技术突破', 'tags': ['DeepSeek', '开源'], 'source_type': 'hn', 'language': 'zh'},
    {'id': 'fallback_3', 'title': '百度发布文心一言5.0', 'summary': '多模态能力大幅提升，API价格比GPT-4o低60%', 'category': '产品发布', 'tags': ['百度', '多模态'], 'source_type': 'cn', 'language': 'zh'},
    {'id': 'fallback_4', 'title': 'Google 发布 Gemini 2.5 Pro', 'summary': '代码能力超越GPT-5，首次集成Android端侧推理', 'category': '产品发布', 'tags': ['Google', 'Gemini'], 'source_type': 'hn', 'language': 'zh'},
    {'id': 'fallback_5', 'title': '字节跳动豆包日均调用突破5000亿Token', 'summary': '成国内最大AI应用平台，API降价50%', 'category': '公司动态', 'tags': ['字节跳动', '豆包'], 'source_type': 'cn', 'language': 'zh'},
    {'id': 'fallback_6', 'title': 'NVIDIA 发布 B300 GPU 架构', 'summary': '推理性能提升12倍，专为大模型推理优化', 'category': '产品发布', 'tags': ['NVIDIA', 'GPU'], 'source_type': 'hn', 'language': 'zh'},
    {'id': 'fallback_7', 'title': 'Anthropic CEO 万字长文谈AI安全', 'summary': '提出负责任扩展框架与独立安全委员会制度', 'category': '观点评论', 'tags': ['Anthropic', 'AI安全'], 'source_type': 'hn', 'language': 'zh'},
    {'id': 'fallback_8', 'title': 'Meta 开源 Llama 4-405B', 'summary': '社区反响两极分化，基准测试争议持续发酵', 'category': '技术突破', 'tags': ['Meta', 'Llama'], 'source_type': 'hn', 'language': 'zh'},
]

_EN_FALLBACK_ARTICLES = [
    {'id': 'fallback_en_1', 'title': 'OpenAI Announces New Funding Round', 'summary': 'Valued at $300B, led by Thrive Capital', 'category': '公司动态', 'tags': ['OpenAI', 'Funding'], 'source_type': 'hn', 'language': 'en'},
    {'id': 'fallback_en_2', 'title': 'DeepSeek Releases Open-Source Model V4', 'summary': 'Performance rivals Claude 4, training cost only $6M', 'category': '技术突破', 'tags': ['DeepSeek', 'Open Source'], 'source_type': 'hn', 'language': 'en'},
    {'id': 'fallback_en_3', 'title': 'Google Launches Gemini 2.5 Pro', 'summary': 'Coding ability surpasses GPT-5, first on-device Android inference', 'category': '产品发布', 'tags': ['Google', 'Gemini'], 'source_type': 'hn', 'language': 'en'},
    {'id': 'fallback_en_4', 'title': 'NVIDIA Unveils B300 GPU Architecture', 'summary': '12x inference performance boost, optimized for LLM inference', 'category': '产品发布', 'tags': ['NVIDIA', 'GPU'], 'source_type': 'hn', 'language': 'en'},
    {'id': 'fallback_en_5', 'title': 'Anthropic CEO on AI Safety', 'summary': 'Proposes responsible scaling framework and independent safety board', 'category': '观点评论', 'tags': ['Anthropic', 'AI Safety'], 'source_type': 'hn', 'language': 'en'},
    {'id': 'fallback_en_6', 'title': 'Meta Open-Sources Llama 4-405B', 'summary': 'Community reaction polarized, benchmark controversy continues', 'category': '技术突破', 'tags': ['Meta', 'Llama'], 'source_type': 'hn', 'language': 'en'},
]


def _load_articles_from_output(lang: str = 'zh') -> list[dict]:
    """从管线 output.json 加载文章，支持语言过滤"""
    output_dir = os.path.join(os.path.dirname(__file__), 'pipeline')
    
    # 根据语言选择不同的 JSON 文件
    if lang == 'en':
        # 尝试读取英文版
        en_json_path = os.path.join(output_dir, 'output_en.json')
        if os.path.exists(en_json_path):
            try:
                with open(en_json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                articles = data.get('articles', [])
                if articles:
                    print(f'[Articles] 从 output_en.json 加载 {len(articles)} 条英文文章')
                    return articles
            except (json.JSONDecodeError, IOError) as e:
                print(f'[Articles] 读取 output_en.json 失败: {e}')
        
        # 降级：从中文版中筛选英文源文章，使用 english_title/english_summary
        json_path = os.path.join(output_dir, 'output.json')
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                all_articles = data.get('articles', [])
                en_articles = []
                for a in all_articles:
                    # 只取有英文字段的文章（海外源）
                    if a.get('english_title') or a.get('language') == 'en':
                        en_articles.append({
                            'id': a.get('id', ''),
                            'title': a.get('english_title') or a.get('title', ''),
                            'summary': a.get('english_summary') or a.get('summary', ''),
                            'category': a.get('category', ''),
                            'tags': a.get('tags', []),
                            'source_type': a.get('source_type', ''),
                            'url': a.get('url', ''),
                            'source_count': a.get('source_count', 1),
                            'language': 'en',
                        })
                if en_articles:
                    print(f'[Articles] 从 output.json 筛选 {len(en_articles)} 条英文文章')
                    return en_articles
            except (json.JSONDecodeError, IOError) as e:
                print(f'[Articles] 读取 output.json 失败: {e}')
        
        return []
    
    # 中文模式（默认）
    json_path = os.path.join(output_dir, 'output.json')
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            articles = data.get('articles', [])
            if articles:
                # 为每篇文章标记语言
                for a in articles:
                    if 'language' not in a:
                        a['language'] = 'zh'
                print(f'[Articles] 从 output.json 加载 {len(articles)} 条文章')
                return articles
        except (json.JSONDecodeError, IOError) as e:
            print(f'[Articles] 读取 output.json 失败: {e}')
    
    return []


def _filter_by_topics(articles: list[dict], topics: list[str]) -> list[dict]:
    """根据用户偏好主题过滤文章"""
    if not topics:
        return articles
    
    # 主题 → 分类/标签映射
    topic_keywords = {
        'AI': ['AI', 'LLM', 'GPT', '大模型', '人工智能', 'DeepSeek', 'OpenAI', 'Anthropic', 'DeepMind', 'Gemini', 'Claude'],
        '科技': ['AI', 'LLM', 'GPU', '芯片', '算力', 'NVIDIA', '模型', '推理', '训练', '开源'],
        '产品': ['产品', '发布', '应用', 'Launch', 'Release', 'API', '工具', '平台'],
        '学术': ['论文', '研究', 'arxiv', '学术', '理论', '方法'],
    }
    
    # 收集用户选择主题对应的关键词
    user_keywords = set()
    for topic in topics:
        keywords = topic_keywords.get(topic, [topic])
        user_keywords.update(k.lower() for k in keywords)
    
    if not user_keywords:
        return articles
    
    filtered = []
    for article in articles:
        # 检查分类
        category = (article.get('category', '') or '').lower()
        # 检查标签
        tags = [t.lower() for t in (article.get('tags', []) or [])]
        # 检查标题
        title = (article.get('title', '') or '').lower()
        
        # 任一匹配即保留
        if any(kw in category for kw in user_keywords):
            filtered.append(article)
        elif any(kw in t for t in tags for kw in user_keywords):
            filtered.append(article)
        elif any(kw in title for kw in user_keywords):
            filtered.append(article)
    
    return filtered if filtered else articles  # 如果全部过滤掉了，返回全部


@app.route('/api/v2/articles', methods=['GET'])
@login_required
def get_articles():
    """获取今日资讯（需登录）"""
    lang = request.args.get('lang', 'zh').strip()
    if lang not in ('zh', 'en'):
        lang = 'zh'
    
    # 读取用户偏好
    user_topics = []
    conn = get_db()
    try:
        prefs = conn.execute(
            "SELECT topics FROM user_preferences WHERE user_id = ?",
            (g.current_user_id,)
        ).fetchone()
        if prefs and prefs['topics']:
            try:
                user_topics = json.loads(prefs['topics'])
            except (json.JSONDecodeError, TypeError):
                user_topics = []
    except Exception as e:
        print(f'[Articles] 读取偏好失败: {e}')
    finally:
        conn.close()
    
    # 加载真实管线数据
    articles = _load_articles_from_output(lang)
    
    # 降级到硬编码假数据
    if not articles:
        print(f'[Articles] output.json 不可用，使用降级数据 (lang={lang})')
        articles = _EN_FALLBACK_ARTICLES if lang == 'en' else _FALLBACK_ARTICLES
    
    # 根据用户偏好过滤
    if user_topics:
        articles = _filter_by_topics(articles, user_topics)
    
    # 也返回 headline 信息
    headline = {}
    output_json_path = os.path.join(BASE_DIR, 'pipeline', 'output.json')
    if os.path.exists(output_json_path):
        try:
            with open(output_json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            hl = data.get('headline', {})
            if isinstance(hl, dict):
                headline = {
                    'title': hl.get('title', ''),
                    'summary': hl.get('summary', ''),
                    'eli5': hl.get('eli5', ''),
                }
        except Exception:
            pass
    
    return jsonify({
        'articles': articles,
        'headline': headline,
        'total': len(articles),
        'language': lang,
    })


# =====================================================================
#  Topic 专题页面（保持原有功能）
# =====================================================================

@app.route('/topic', methods=['GET'])
def topic_page():
    """动态专题页面"""
    output_json_path = os.path.join(BASE_DIR, 'pipeline', 'output.json')
    articles = []

    if os.path.exists(output_json_path):
        try:
            with open(output_json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            articles = data.get('articles', [])
            headline = data.get('headline', {})
        except (json.JSONDecodeError, IOError) as e:
            print(f'[Topic] 读取 output.json 失败: {e}')
            articles = []
            headline = {}

    if not articles:
        headline = {
            'title': 'AI 行业每日动态',
            'summary': '硅谷AI晨报 · 深度追踪AI行业最重要的议题'
        }
        articles = [
            {'title': 'OpenAI 宣布完成新一轮融资', 'summary': '估值3000亿美元，由Thrive Capital领投', 'category': '公司动态', 'tags': ['OpenAI', '融资']},
            {'title': 'DeepSeek 开源最新模型 V4', 'summary': '性能对标 Claude 4，训练成本仅为600万美元', 'category': '技术突破', 'tags': ['DeepSeek', '开源']},
            {'title': '百度发布文心一言5.0', 'summary': '多模态能力大幅提升，API价格比GPT-4o低60%', 'category': '产品发布', 'tags': ['百度', '多模态']},
            {'title': 'Google 发布 Gemini 2.5 Pro', 'summary': '代码能力超越GPT-5，首次集成Android端侧推理', 'category': '产品发布', 'tags': ['Google', 'Gemini']},
            {'title': '字节跳动豆包日均调用突破5000亿Token', 'summary': '成国内最大AI应用平台，API降价50%', 'category': '公司动态', 'tags': ['字节跳动', '豆包']},
            {'title': 'NVIDIA 发布 B300 GPU 架构', 'summary': '推理性能提升12倍，专为大模型推理优化', 'category': '产品发布', 'tags': ['NVIDIA', 'GPU']},
            {'title': 'Anthropic CEO 万字长文谈AI安全', 'summary': '提出负责任扩展框架与独立安全委员会制度', 'category': '观点评论', 'tags': ['Anthropic', 'AI安全']},
            {'title': 'Meta 开源 Llama 4-405B', 'summary': '社区反响两极分化，基准测试争议持续发酵', 'category': '技术突破', 'tags': ['Meta', 'Llama']},
        ]

    # 按分类分组
    category_order = ['公司动态', '技术突破', '产品发布', '观点评论', '学术前沿']
    grouped = {}
    for a in articles:
        cat = a.get('category', '未分类')
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(a)

    sorted_cats = sorted(grouped.keys(), key=lambda c: (
        category_order.index(c) if c in category_order else 99,
        -len(grouped[c])
    ))

    def esc(s):
        if not isinstance(s, str):
            s = str(s)
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    cat_colors = {
        '公司动态': '#B87333', '技术突破': '#8B5E3C', '产品发布': '#A0522D',
        '观点评论': '#6B4226', '学术前沿': '#CD853F', '未分类': '#C4A06A',
    }
    cat_icons = {
        '公司动态': '🏢', '技术突破': '🔬', '产品发布': '🚀',
        '观点评论': '💬', '学术前沿': '📚', '未分类': '📌',
    }

    page_title = esc(headline.get('title', 'AI行业每日动态')) if headline else 'AI行业每日动态'
    page_subtitle = esc(headline.get('summary', '硅谷AI晨报 · 深度追踪AI行业最重要的议题')) if headline else '硅谷AI晨报 · 深度追踪AI行业最重要的议题'

    sections_html = ''
    for cat in sorted_cats:
        cat_articles = grouped[cat]
        color = cat_colors.get(cat, '#C4A06A')
        icon = cat_icons.get(cat, '📌')
        sections_html += f'<div class="section">'
        sections_html += f'<div class="section-header" style="border-left-color:{color}">'
        sections_html += f'<span class="section-icon">{icon}</span>'
        sections_html += f'<span class="section-title" style="color:{color}">{esc(cat)}</span>'
        sections_html += f'<span class="section-count">{len(cat_articles)}条</span>'
        sections_html += '</div>'

        for a in cat_articles:
            title = esc(a.get('title', '无标题'))
            summary = esc(a.get('summary', a.get('ai_comment', '')))
            if not summary:
                summary = '暂无摘要'
            tags = a.get('tags', a.get('source_tags', []))
            url = a.get('url', '')
            author = a.get('author', '')
            source_type = a.get('source_type', '')

            source_badge = ''
            source_label_map = {'x': '𝕏', 'yt': '📺 YT', 'hf': '🤗 HF', 'hn': '💬 HN', 'arx': '📄 ArXiv'}
            if source_type:
                label = source_label_map.get(source_type, source_type.upper())
                source_badge = f'<span class="source-badge">{esc(label)}</span>'

            sections_html += '<div class="card">'
            sections_html += '<div class="card-meta">'
            if source_badge:
                sections_html += source_badge
            if author:
                sections_html += f'<span class="card-author">{esc(author)}</span>'
            sections_html += '</div>'

            sections_html += f'<h2 class="card-title">'
            if url:
                sections_html += f'<a href="{esc(url)}" target="_blank" rel="noopener">{title}</a>'
            else:
                sections_html += title
            sections_html += '</h2>'

            sections_html += f'<p class="card-summary">{summary}</p>'

            if tags:
                sections_html += '<div class="tags">'
                for t in tags:
                    sections_html += f'<span class="tag">{esc(t)}</span>'
                sections_html += '</div>'

            sections_html += '</div>'

        sections_html += '</div>'

    data_sources = set()
    for a in articles:
        st = a.get('source_type', '')
        if st:
            data_sources.add(st)
    source_note = ' · '.join(sorted(data_sources)) if data_sources else '多源聚合'
    total_count = len(articles)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>{page_title} - 硅谷AI晨报</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: #FAF7F2;
    color: #3E2C1C;
    padding: 0;
    max-width: 680px;
    margin: 0 auto;
    line-height: 1.6;
}}
.page-header {{
    background: linear-gradient(135deg, #5C3317 0%, #8B5E3C 50%, #A0683A 100%);
    color: #FFF;
    padding: 32px 20px 28px;
    text-align: center;
    border-bottom: 3px solid #C4A06A;
}}
.page-header h1 {{
    font-size: 24px;
    font-weight: 700;
    letter-spacing: 1px;
    margin-bottom: 6px;
}}
.page-header .subtitle {{
    font-size: 13px;
    opacity: 0.85;
    color: #F0E0C8;
    margin-top: 4px;
}}
.page-header .meta-line {{
    margin-top: 10px;
    font-size: 11px;
    color: #D4B896;
}}
.content {{ padding: 12px 14px 30px; }}
.section {{ margin-bottom: 20px; }}
.section-header {{
    display: flex;
    align-items: center;
    gap: 6px;
    border-left: 4px solid #B87333;
    padding: 6px 0 6px 10px;
    margin-bottom: 10px;
    margin-left: 2px;
}}
.section-icon {{ font-size: 16px; }}
.section-title {{
    font-size: 16px;
    font-weight: 700;
    color: #8B5E3C;
}}
.section-count {{
    font-size: 11px;
    color: #A68A6B;
    background: #F0E8D8;
    padding: 1px 8px;
    border-radius: 10px;
    margin-left: 4px;
}}
.card {{
    background: #FFFFFF;
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 10px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.05);
    border-left: 3px solid transparent;
    transition: border-color 0.2s;
}}
.card:hover {{ border-left-color: #C4A06A; }}
.card-meta {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    flex-wrap: wrap;
}}
.source-badge {{
    font-size: 10px;
    background: #F5ECE0;
    color: #8B7355;
    padding: 1px 7px;
    border-radius: 8px;
    letter-spacing: 0.3px;
}}
.card-author {{
    font-size: 11px;
    color: #A68A6B;
}}
.card-title {{
    font-size: 16px;
    font-weight: 600;
    color: #5C3317;
    margin-bottom: 6px;
    line-height: 1.4;
}}
.card-title a {{
    color: #5C3317;
    text-decoration: none;
    border-bottom: 1px dashed transparent;
    transition: border-color 0.2s;
}}
.card-title a:hover {{
    border-bottom-color: #C4A06A;
}}
.card-summary {{
    font-size: 13px;
    line-height: 1.7;
    color: #6B5D4F;
    margin-bottom: 6px;
}}
.tags {{
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
}}
.tag {{
    display: inline-block;
    background: #F0E8D8;
    color: #8B5E3C;
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 10px;
    letter-spacing: 0.2px;
}}
.footer {{
    text-align: center;
    padding: 24px 20px;
    color: #A68A6B;
    font-size: 11px;
    border-top: 1px solid #E8DCC8;
    margin-top: 10px;
}}
.footer a {{ color: #8B5E3C; text-decoration: none; }}
@media (max-width: 480px) {{
    .page-header {{ padding: 22px 14px 20px; }}
    .page-header h1 {{ font-size: 20px; }}
    .content {{ padding: 8px 8px 20px; }}
    .card {{ padding: 12px 12px; }}
    .card-title {{ font-size: 15px; }}
    .card-summary {{ font-size: 12px; }}
    .section-title {{ font-size: 15px; }}
}}
</style>
</head>
<body>

<div class="page-header">
    <h1>{page_title}</h1>
    <p class="subtitle">{page_subtitle}</p>
    <p class="meta-line">共 {total_count} 条资讯 · {source_note}</p>
</div>

<div class="content">
{sections_html}
</div>

<div class="footer">
    <p>📰 硅谷AI晨报 · 专题持续更新</p>
    <p style="margin-top:4px">数据来源：{source_note} · <a href="https://hermes-agent.nousresearch.com" target="_blank">Powered by Nous Research</a></p>
</div>

</body>
</html>'''
    return html


# =====================================================================
#  OPTIONS 预检请求
# =====================================================================

@app.route('/api/<path:path>', methods=['OPTIONS'])
@app.route('/api/auth/<path:path>', methods=['OPTIONS'])
@app.route('/api/user/<path:path>', methods=['OPTIONS'])
def handle_options(path=None):
    """处理 CORS 预检请求"""
    return '', 204


# =====================================================================
#  启动
# =====================================================================

def main():
    """启动 Parro Flask 服务器"""
    print("=" * 60)
    print("  🦜 Parro 后端服务器")
    print("=" * 60)
    print(f"  📡 监听地址: http://0.0.0.0:{PORT}")
    print(f"  📁 静态目录: {BASE_DIR}")
    print(f"  🔑 VAPID Key: {VAPID_PUBLIC_KEY[:30]}...")
    print(f"  👤 审核账号: demo@parro.app")
    print("=" * 60)
    print(f"\n  🌐 访问地址:")
    print(f"     - 本机: http://localhost:{PORT}")
    print(f"     - 局域网: http://<你的IP>:{PORT}")
    print(f"\n  📋 API 端点:")
    print(f"     POST /api/auth/register       用户注册")
    print(f"     POST /api/auth/login          用户登录")
    print(f"     POST /api/auth/logout         用户登出")
    print(f"     GET  /api/auth/me             当前用户信息")
    print(f"     GET  /api/auth/google/url     Google OAuth 授权URL")
    print(f"     GET  /api/auth/google/callback Google OAuth 回调")
    print(f"     POST /api/auth/apple/callback Apple Sign-In 回调")
    print(f"     GET  /api/user/preferences    获取偏好设置")
    print(f"     PUT  /api/user/preferences    更新偏好设置")
    print(f"     GET  /api/vapid-public-key    获取推送公钥")
    print(f"     POST /api/push/subscribe      订阅推送")
    print(f"     POST /api/push/unsubscribe    取消订阅")
    print(f"     GET  /api/v2/voice/list       语音列表（8预设）")
    print(f"     POST /api/v2/voice/tts        生成TTS语音（edge-tts）")
    print(f"     POST /api/v2/voice/clone      语音克隆（需API Key）")
    print(f"     GET  /api/v2/voice/clones     已克隆语音列表")
    print(f"     DELETE /api/v2/voice/clone/<id> 删除克隆语音")
    print(f"     GET  /api/v2/articles         今日资讯")
    print(f"\n  ⚠️  按 Ctrl+C 停止服务器\n")

    app.run(host='0.0.0.0', port=PORT, debug=False)


if __name__ == '__main__':
    main()
