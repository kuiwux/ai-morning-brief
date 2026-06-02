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

from flask import Flask, request, jsonify, send_from_directory, g
from database import init_db, create_demo_account, get_db
from auth import login_required, hash_password, verify_password, create_token, decode_token, blacklist_token

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
with app.app_context():
    init_db()
    create_demo_account()


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

@app.route('/api/v2/voice/list', methods=['GET'])
def voice_list():
    """获取可用语音列表"""
    voices = [
        {
            'voice_id': 'edge_yunxi',
            'name': '云希',
            'display_name': '男播音员',
            'gender': 'male',
            'style': '沉稳大气',
            'avatar': '🎤',
            'avatar_class': 'vca-male',
            'sample_text': '欢迎使用硅谷AI晨报语音播报，我是云希，每天五分钟，掌握AI圈最新动态。'
        },
        {
            'voice_id': 'edge_xiaoxiao',
            'name': '晓晓',
            'display_name': '女播音员',
            'gender': 'female',
            'style': '温柔清晰',
            'avatar': '🎙️',
            'avatar_class': 'vca-female',
            'sample_text': '欢迎使用硅谷AI晨报语音播报，我是晓晓，每天五分钟，掌握AI圈最新动态。'
        },
        {
            'voice_id': 'edge_yunjian',
            'name': '云健',
            'display_name': '电影解说',
            'gender': 'male',
            'style': '戏谑幽默',
            'avatar': '🎬',
            'avatar_class': 'vca-movie',
            'sample_text': '欢迎使用硅谷AI晨报语音播报，我是云健，每天五分钟，掌握AI圈最新动态。'
        },
        {
            'voice_id': 'edge_yunyang',
            'name': '云扬',
            'display_name': '技术极客',
            'gender': 'male',
            'style': '快速理性',
            'avatar': '🤖',
            'avatar_class': 'vca-geek',
            'sample_text': '欢迎使用硅谷AI晨报语音播报，我是云扬，每天五分钟，掌握AI圈最新动态。'
        }
    ]
    return jsonify({'voices': voices})


@app.route('/api/v2/voice/tts', methods=['POST'])
def voice_tts():
    """生成 TTS 语音"""
    data = request.get_json(silent=True) or {}
    text = data.get('text', '')
    voice_id = data.get('voice_id', 'edge_yunxi')
    speed = float(data.get('speed', 1.0))

    if not text:
        return jsonify({'error': '缺少 text 参数'}), 400

    print(f'[TTS] 生成语音: voice={voice_id}, speed={speed}, text_len={len(text)}')

    # 尝试调用外部 TTS API
    tts_url = os.environ.get('TTS_API_URL', '')
    tts_token = os.environ.get('TTS_API_TOKEN', '')

    if tts_url:
        try:
            import urllib.request
            import urllib.error

            req_body = json.dumps({
                'text': text,
                'voice_id': voice_id,
                'speed': speed
            }).encode('utf-8')

            req = urllib.request.Request(tts_url, data=req_body, method='POST')
            req.add_header('Content-Type', 'application/json')
            if tts_token:
                req.add_header('Authorization', f'Bearer {tts_token}')

            with urllib.request.urlopen(req, timeout=30) as resp:
                audio_data = resp.read()
                print(f'[TTS] 外部 API 生成 {len(audio_data)} 字节')
                from flask import Response
                return Response(audio_data, mimetype='audio/mpeg')
        except Exception as e:
            print(f'[TTS] 外部 API 失败: {e}，回退到静默音频')

    # 回退：生成静默 MP3
    silent_mp3 = bytes([
        0xFF, 0xFB, 0x90, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
    ])
    mp3_frames = []
    header = bytes([0xFF, 0xFB, 0x90, 0x00])
    for i in range(80):
        mp3_frames.append(header)
        mp3_frames.append(b'\x00' * 413)

    audio = b''.join(mp3_frames)
    print(f'[TTS] 生成静默音频 {len(audio)} 字节')
    from flask import Response
    return Response(audio, mimetype='audio/mpeg')


# =====================================================================
#  资讯 API
# =====================================================================

@app.route('/api/v2/articles', methods=['GET'])
def get_articles():
    """获取今日资讯"""
    articles_data = [
        {'title': 'OpenAI 宣布完成新一轮融资', 'summary': '估值3000亿美元，由Thrive Capital领投'},
        {'title': 'DeepSeek 开源最新模型 V4', 'summary': '性能对标 Claude 4，训练成本仅为600万美元'},
        {'title': '百度发布文心一言5.0', 'summary': '多模态能力大幅提升，API价格比GPT-4o低60%'},
        {'title': 'Google 发布 Gemini 2.5 Pro', 'summary': '代码能力超越GPT-5，首次集成Android端侧推理'},
        {'title': '字节跳动豆包日均调用突破5000亿Token', 'summary': '成国内最大AI应用平台，API降价50%'},
        {'title': 'NVIDIA 发布 B300 GPU 架构', 'summary': '推理性能提升12倍，专为大模型推理优化'},
        {'title': 'Anthropic CEO 万字长文谈AI安全', 'summary': '提出负责任扩展框架与独立安全委员会制度'},
        {'title': 'Meta 开源 Llama 4-405B', 'summary': '社区反响两极分化，基准测试争议持续发酵'}
    ]
    return jsonify({'articles': articles_data})


# =====================================================================
#  Topic 专题页面（保持原有功能）
# =====================================================================

@app.route('/topic', methods=['GET'])
def topic_page():
    """动态专题页面"""
    output_json_path = '/tmp/ai_morning_brief/output.json'
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
    print(f"     GET  /api/user/preferences    获取偏好设置")
    print(f"     PUT  /api/user/preferences    更新偏好设置")
    print(f"     GET  /api/vapid-public-key    获取推送公钥")
    print(f"     POST /api/push/subscribe      订阅推送")
    print(f"     POST /api/push/unsubscribe    取消订阅")
    print(f"     GET  /api/v2/voice/list       语音列表")
    print(f"     POST /api/v2/voice/tts        生成TTS语音")
    print(f"     GET  /api/v2/articles         今日资讯")
    print(f"\n  ⚠️  按 Ctrl+C 停止服务器\n")

    app.run(host='0.0.0.0', port=PORT, debug=False)


if __name__ == '__main__':
    main()
