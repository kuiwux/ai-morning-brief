#!/usr/bin/env python3
"""
硅谷AI晨报 PWA — 后端服务器
端口: 8899
功能:
  - 静态文件服务（index.html, manifest.json, sw.js, icons, push-notification.js）
  - VAPID 公钥端点 GET /api/vapid-public-key
  - 推送订阅端点 POST /api/push/subscribe
  - 推送取消订阅端点 POST /api/push/unsubscribe
  - 正确的 Content-Type 和 CORS 头
"""

import json
import os
import http.server
import socketserver
import ssl

PORT = 8899
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ===== VAPID Keys (DEMO - 实际部署时替换为真实密钥) =====
# 生成方式: openssl ecparam -genkey -name prime256v1 -out private.pem
# 然后从密钥对提取公钥
VAPID_PUBLIC_KEY = "BDJqNKhTZQHt6GbKpMxPqS3vW8yL2nR4xYcF7aB1dE5gH9iJ0kLmN3oP6qR5sT7uV8wX1yZ2aB3cD4eF5gH6i"

# Store push subscriptions (in production, use a database)
push_subscriptions = []


class PWARequestHandler(http.server.SimpleHTTPRequestHandler):
    """Custom request handler with PWA support."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    # ===== CORS Headers =====
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.end_headers()

    # ===== API Routes =====
    def do_GET(self):
        """Handle GET requests."""
        path = self.path.split('?')[0]  # Strip query params

        # API: VAPID public key
        if path == '/api/vapid-public-key':
            self.send_json_response({
                'publicKey': VAPID_PUBLIC_KEY
            })
            return

        # API: List subscriptions (for debugging)
        if path == '/api/push/subscriptions':
            self.send_json_response({
                'count': len(push_subscriptions),
                'subscriptions': push_subscriptions
            })
            return

        # === Voice & TTS APIs ===

        # API: List available voices
        if path == '/api/v2/voice/list':
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
            self.send_json_response({'voices': voices})
            return

        # API: Get today's articles
        if path == '/api/v2/articles':
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
            self.send_json_response({'articles': articles_data})
            return

        # Topic page (linked from WeChat article)
        if path == '/topic':
            topic_html = self._build_topic_page()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(topic_html)))
            self.end_headers()
            self.wfile.write(topic_html)
            return

        # Serve static files
        super().do_GET()

    def do_POST(self):
        """Handle POST requests."""
        path = self.path.split('?')[0]

        # Read request body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else b'{}'

        try:
            data = json.loads(body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}

        # API: Subscribe to push
        if path == '/api/push/subscribe':
            subscription = {
                'endpoint': data.get('endpoint', ''),
                'keys': data.get('keys', {}),
                'userAgent': data.get('userAgent', ''),
                'timestamp': data.get('timestamp', '')
            }

            # Deduplicate: remove existing subscription with same endpoint
            global push_subscriptions
            push_subscriptions = [
                s for s in push_subscriptions
                if s.get('endpoint') != subscription['endpoint']
            ]
            push_subscriptions.append(subscription)

            print(f'[Push] New subscription added. Total: {len(push_subscriptions)}')
            self.send_json_response({
                'status': 'ok',
                'message': 'Subscribed successfully'
            })
            return

        # API: Unsubscribe from push
        if path == '/api/push/unsubscribe':
            endpoint = data.get('endpoint', '')
            push_subscriptions[:] = [
                s for s in push_subscriptions
                if s.get('endpoint') != endpoint
            ]
            print(f'[Push] Subscription removed. Total: {len(push_subscriptions)}')
            self.send_json_response({
                'status': 'ok',
                'message': 'Unsubscribed successfully'
            })
            return

        # === TTS API: Generate speech ===
        if path == '/api/v2/voice/tts':
            text = data.get('text', '')
            voice_id = data.get('voice_id', 'edge_yunxi')
            speed = float(data.get('speed', 1.0))

            if not text:
                self.send_json_response({'error': 'Missing text parameter'}, status=400)
                return

            print(f'[TTS] Generating speech: voice={voice_id}, speed={speed}, text_len={len(text)}')

            # Try to call an external TTS API if configured
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
                        self.send_response(200)
                        self.send_header('Content-Type', 'audio/mpeg')
                        self.send_header('Content-Length', str(len(audio_data)))
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(audio_data)
                        print(f'[TTS] Generated {len(audio_data)} bytes via external API')
                        return
                except Exception as e:
                    print(f'[TTS] External API failed: {e}, falling back to dummy audio')

            # Fallback: return a tiny silent MP3 so the frontend can demo the flow
            # Minimal valid MP3 frame (silence)
            silent_mp3 = bytes([
                0xFF, 0xFB, 0x90, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
            ])
            # Build a short silence MP3
            import struct
            mp3_frames = []
            # Generate ~2 seconds of silence MP3 frames
            header = bytes([0xFF, 0xFB, 0x90, 0x00])
            for i in range(80):
                mp3_frames.append(header)
                mp3_frames.append(b'\x00' * 413)  # padding for 128kbps frames

            audio = b''.join(mp3_frames)
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Content-Length', str(len(audio)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(audio)
            print(f'[TTS] Generated {len(audio)} bytes (dummy silence)')
            return

        # Default: 404
        self.send_json_response({
            'error': 'Not found'
        }, status=404)

    # ===== Helpers =====
    def _build_topic_page(self):
        """Build a dynamic featured topic page from real daily brief data."""
        # --- 1. Load articles ---
        output_json_path = '/tmp/ai_morning_brief/output.json'
        articles = []

        if os.path.exists(output_json_path):
            try:
                with open(output_json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                articles = data.get('articles', [])
                headline = data.get('headline', {})
            except (json.JSONDecodeError, IOError) as e:
                print(f'[Topic] Failed to read output.json: {e}')
                articles = []
                headline = {}

        # Fallback: use hardcoded data
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

        # --- 2. Group articles by category ---
        category_order = ['公司动态', '技术突破', '产品发布', '观点评论', '学术前沿']
        grouped = {}
        for a in articles:
            cat = a.get('category', '未分类')
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append(a)

        # Sort categories by predefined order, then by count
        sorted_cats = sorted(grouped.keys(), key=lambda c: (
            category_order.index(c) if c in category_order else 99,
            -len(grouped[c])
        ))

        # --- 3. Build HTML ---
        def esc(s):
            """Minimal HTML escape for text content."""
            if not isinstance(s, str):
                s = str(s)
            return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

        # Category accent colors (warm palette)
        cat_colors = {
            '公司动态': '#B87333',
            '技术突破': '#8B5E3C',
            '产品发布': '#A0522D',
            '观点评论': '#6B4226',
            '学术前沿': '#CD853F',
            '未分类': '#C4A06A',
        }
        cat_icons = {
            '公司动态': '🏢',
            '技术突破': '🔬',
            '产品发布': '🚀',
            '观点评论': '💬',
            '学术前沿': '📚',
            '未分类': '📌',
        }

        page_title = esc(headline.get('title', 'AI行业每日动态')) if headline else 'AI行业每日动态'
        page_subtitle = esc(headline.get('summary', '硅谷AI晨报 · 深度追踪AI行业最重要的议题')) if headline else '硅谷AI晨报 · 深度追踪AI行业最重要的议题'

        # Build category sections
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

                # Source badge
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

        # Data source note
        data_sources = set()
        for a in articles:
            st = a.get('source_type', '')
            if st:
                data_sources.add(st)
        source_note = ' · '.join(sorted(data_sources)) if data_sources else '多源聚合'
        total_count = len(articles)

        # --- 4. Full HTML ---
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

/* ── Header ── */
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

/* ── Sections & Cards ── */
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

/* ── Footer ── */
.footer {{
    text-align: center;
    padding: 24px 20px;
    color: #A68A6B;
    font-size: 11px;
    border-top: 1px solid #E8DCC8;
    margin-top: 10px;
}}
.footer a {{ color: #8B5E3C; text-decoration: none; }}

/* ── Mobile ── */
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
        return html.encode('utf-8')
    def send_json_response(self, data, status=200):
        """Send a JSON response."""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    # ===== Override content-type guessing for PWA files =====
    def guess_type(self, path):
        """Guess the MIME type with PWA-specific overrides."""
        base, ext = os.path.splitext(path)

        mime_map = {
            '.js': 'application/javascript; charset=utf-8',
            '.json': 'application/json; charset=utf-8',
            '.html': 'text/html; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.png': 'image/png',
            '.svg': 'image/svg+xml',
            '.ico': 'image/x-icon',
            '.woff2': 'font/woff2',
        }

        if ext in mime_map:
            return mime_map[ext]

        return super().guess_type(path)

    # ===== Logging =====
    def log_message(self, format, *args):
        """Custom log format with emoji indicators."""
        emoji = ''
        if hasattr(self, 'command'):
            if self.command == 'GET':
                emoji = '📥'
            elif self.command == 'POST':
                emoji = '📤'
        print(f'{emoji} {self.client_address[0]} - {format % args}')


def main():
    """Start the PWA server."""
    os.chdir(BASE_DIR)

    # Allow address reuse
    socketserver.TCPServer.allow_reuse_address = True

    with socketserver.TCPServer(("0.0.0.0", PORT), PWARequestHandler) as httpd:
        print("=" * 60)
        print("  硅谷AI晨报 PWA 服务器")
        print("=" * 60)
        print(f"  📡 监听地址: http://0.0.0.0:{PORT}")
        print(f"  📁 静态目录: {BASE_DIR}")
        print(f"  🔑 VAPID Key: {VAPID_PUBLIC_KEY[:30]}...")
        print(f"  📱 PWA 可安装 · 支持离线缓存 · 推送通知")
        print("=" * 60)
        print(f"\n  🌐 访问地址:")
        print(f"     - 本机: http://localhost:{PORT}")
        print(f"     - 局域网: http://<你的IP>:{PORT}")
        print(f"\n  📋 API 端点:")
        print(f"     GET  /api/vapid-public-key   获取推送公钥")
        print(f"     POST /api/push/subscribe     订阅推送")
        print(f"     POST /api/push/unsubscribe   取消订阅")
        print(f"     GET  /api/v2/voice/list      获取语音列表")
        print(f"     POST /api/v2/voice/tts       生成TTS语音")
        print(f"     GET  /api/v2/articles        获取今日资讯")
        print(f"\n  ⚠️  按 Ctrl+C 停止服务器\n")

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  👋 服务器已停止")
            httpd.server_close()


if __name__ == '__main__':
    main()
