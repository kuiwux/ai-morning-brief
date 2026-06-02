#!/usr/bin/env python3
"""
每日YouTube深度访谈专栏
- 每天下午14:00执行
- 抓取AI领域深度访谈/对话视频
- DeepSeek撰写一篇深度文章（核心观点加粗）
- 推送到微信公众号草稿

工作目录：/tmp/ai_morning_brief/
"""

# Python 3.10+ 兼容性修复：hyperframe 依赖已移除的 collections.MutableSet
import collections.abc
import collections
for _attr in ("MutableSet", "MutableMapping"):
    if not hasattr(collections, _attr):
        setattr(collections, _attr, getattr(collections.abc, _attr))

import os
import re
import sys
import json
import time
import base64
import shutil
import hashlib
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── 路径 ──────────────────────────────────────────────
WORKDIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(WORKDIR)
sys.path.insert(0, WORKDIR)

from wechat_api import get_access_token, create_draft, upload_cover, publish_draft, _minify_html
from pipeline import (
    get_deepseek_client, DEEPSEEK_MODEL, PROXY_URL,
    _parse_srt_text, _parse_json_response,
)

# ── 日志 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("yt_interview")

# ── 代理 ──────────────────────────────────────────────
os.environ.setdefault("http_proxy", PROXY_URL)
os.environ.setdefault("https_proxy", PROXY_URL)

# ══════════════════════════════════════════════════════
# YouTube 频道列表（三级梯队）
# ══════════════════════════════════════════════════════

TIER1_INTERVIEW_CHANNELS = {
    "Lex Fridman":       "@lexfridman",
    "Dwarkesh Patel":    "@DwarkeshPatel",
    "Bloomberg Originals": "@Bloomberg",
    "a16z":              "@a16z",
    "The Ezra Klein Show": "@TheEzraKleinShow",
}

TIER2_TECH_TALKS = {
    "Y Combinator":      "@ycombinator",
    "Stanford Online":   "@stanfordonline",
    "TED":               "@TED",
}

TIER3_OFFICIAL = {
    "OpenAI":            "@OpenAI",
    "Google DeepMind":   "@GoogleDeepMind",
    "Anthropic":         "@AnthropicAI",
    "NVIDIA":            "@NVIDIA",
    "AI Explained":      "@aiexplained-official",
    "Yannic Kilcher":    "@YannicKilcher",
}

# ── 配置 ──────────────────────────────────────────────
YT_SUBS_DIR = "/tmp/yt_interview_subs"
MAX_VIDEOS_PER_CHANNEL = 2          # 每个频道最新2个视频
INTERVIEW_MIN_DURATION = 30 * 60     # 最少30分钟
INTERVIEW_KEYWORDS = [
    "interview", "conversation", "talk", "dialogue",
    "podcast", "fireside", "keynote", "对话", "访谈",
    "深度", "对谈", "专访",
]

# AI 关键词：标题匹配用（精确，避免维京人视频被选）
AI_KEYWORDS_TITLE = [
    "ai ", "artificial intelligence", "machine learning", "deep learning",
    "llm", "gpt", "chatgpt", "openai", "deepmind", "anthropic",
    "neural network", "transformer", "agi", "alignment",
    "reinforcement learning", "robot", "robotics",
    "gpu", "nvidia", "superintelligence",
]

# AI 关键词：字幕匹配用（宽泛）
AI_KEYWORDS_SUBS = [
    "ai ", "artificial intelligence", "machine learning", "deep learning",
    "llm", "gpt", "chatgpt", "openai", "deepmind", "anthropic",
    "neural network", "transformer", "agi", "alignment",
    "reinforcement learning", "rlhf", "diffusion", "generative",
    "copilot", "claude", "gemini", "llama", "mistral",
    "robot", "robotics", "autonomous", "self-driving",
    "gpu", "nvidia", "cuda", "tpu",
    "ai safety", "superintelligence", "singularity",
    "large language model", "foundation model",
]

# ══════════════════════════════════════════════════════
# 视频抓取
# ══════════════════════════════════════════════════════

def fetch_channel_videos(channel_name: str, handle: str, max_videos: int = 2) -> list[dict]:
    """获取频道最新视频并下载字幕。返回 [{channel_name, title, subtitle_text, url, video_id, duration}]"""
    os.makedirs(YT_SUBS_DIR, exist_ok=True)
    channel_url = f"https://www.youtube.com/@{handle.lstrip('@')}"
    videos = []

    try:
        # 获取视频ID列表
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--playlist-end", str(max_videos),
             "--print", "%(id)s\t%(title)s\t%(duration)s",
             "--proxy", PROXY_URL,
             channel_url],
            capture_output=True, text=True, timeout=60,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "429" in stderr:
                logger.warning(f"  ⚠ {channel_name}: 429 限流")
            return videos

        for line in result.stdout.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            video_id, title = parts[0], parts[1]
            duration = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

            if not video_id:
                continue

            video_url = f"https://www.youtube.com/watch?v={video_id}"

            # 下载字幕
            sub_output = os.path.join(YT_SUBS_DIR, "%(id)s")
            sub_result = subprocess.run(
                ["yt-dlp", "--write-auto-subs", "--sub-lang", "en",
                 "--skip-download", "--convert-subs", "srt",
                 "-o", sub_output,
                 "--proxy", PROXY_URL,
                 video_url],
                capture_output=True, text=True, timeout=90,
            )

            if sub_result.returncode != 0:
                logger.warning(f"  ⚠ {channel_name}: {title[:40]}... 字幕下载失败")
                continue

            srt_file = os.path.join(YT_SUBS_DIR, f"{video_id}.en.srt")
            subtitle_text = _parse_srt_text(srt_file) if os.path.exists(srt_file) else ""

            if not subtitle_text:
                continue

            videos.append({
                "channel_name": channel_name,
                "title": title,
                "subtitle_text": subtitle_text[:6000],  # 限制长度
                "url": video_url,
                "video_id": video_id,
                "duration": duration,
            })
            logger.info(f"  ✓ {channel_name}: {title[:50]}... ({len(subtitle_text)} 字)")

    except subprocess.TimeoutExpired:
        logger.warning(f"  ⚠ {channel_name}: 超时")
    except Exception as e:
        logger.warning(f"  ⚠ {channel_name}: {e}")

    return videos


def is_interview(video: dict) -> tuple[bool, int]:
    """判定是否为深度访谈，返回 (is_interview, score)"""
    score = 0
    title = video.get("title", "").lower()
    duration = video.get("duration", 0)

    # 时长
    if duration >= 60 * 60:
        score += 3
    elif duration >= INTERVIEW_MIN_DURATION:
        score += 2
    elif duration >= 15 * 60:
        score += 1

    # 标题关键词
    for kw in INTERVIEW_KEYWORDS:
        if kw.lower() in title:
            score += 2
            break

    # 默认通过：知名访谈频道
    channel = video.get("channel_name", "")
    if channel in TIER1_INTERVIEW_CHANNELS:
        score += 2

    # AI 相关性（必须在标题中命中）
    title_lower = title
    ai_hit = any(kw in title_lower for kw in AI_KEYWORDS_TITLE)
    if not ai_hit:
        # 放宽：标题没命中，但字幕中多次出现 AI 关键词也行
        subtitle = video.get("subtitle_text", "").lower()
        hits = sum(1 for kw in AI_KEYWORDS_SUBS if kw in subtitle)
        if hits >= 3:
            ai_hit = True
            score += 1
        else:
            return False, 0  # 非AI内容

    score += 1  # AI相关性加分

    return score >= 2, score


# ══════════════════════════════════════════════════════
# DeepSeek 深度文章 Prompt
# ══════════════════════════════════════════════════════

DEEP_ARTICLE_PROMPT = """你是一位资深AI行业分析师和科技专栏作家，负责撰写「硅谷AI深度」专栏。

## 任务
根据以下YouTube访谈/对话的字幕内容，撰写一篇**深度分析文章**。

## 原始字幕
{transcript}

## 文章要求

### 结构
1. **标题**（15-30字）：提炼访谈最核心的洞察或争议点
2. **导语**（80-120字）：一句话说清这是谁的访谈、为什么值得关注
3. **核心观点**（3-5个）：每个观点一个小标题 + 150-200字分析
   - 用 **加粗** 突出每个观点的核心结论
   - 引用访谈中的原话或关键数据
   - 联系行业背景，说明为什么这个观点重要
4. **编辑视角**（80-100字）：润之的一句话总结，点出最有价值的takeaway

### 风格
- 叙事性强，像杂志专栏，不是快讯堆砌
- 中文优雅流畅，保留技术名词英文原名
- 全文字数：1200-1800字
- 加粗比例控制在全文15%以内

### 输出格式
必须只输出一个纯JSON对象，不要markdown代码块：

{{
  "title": "文章标题",
  "subtitle": "副标题/导语",
  "speaker": "访谈主角姓名",
  "speaker_title": "主角身份/职位",
  "source": "频道名",
  "sections": [
    {{
      "heading": "核心观点小标题",
      "body": "分析段落，**核心结论加粗**，150-200字"
    }}
  ],
  "editor_note": "润之的一句话总结"
}}
"""


def call_deepseek_article(transcript: str) -> dict:
    """调用 DeepSeek 撰写深度文章"""
    prompt = DEEP_ARTICLE_PROMPT.format(transcript=transcript[:5000])
    client = get_deepseek_client()

    logger.info("🤖 正在调用 DeepSeek 撰写深度文章...")
    response = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {
                "role": "system",
                "content": "你是一位资深科技专栏作家，擅长从长篇访谈中提炼关键洞察，写出有深度、有观点的文章。"
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_tokens=16384,
    )

    content = response.choices[0].message.content
    logger.info(f"🤖 DeepSeek 返回 {len(content)} 字符")
    # 保存原始响应用于调试
    debug_path = os.path.join(WORKDIR, f"deepseek_raw_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(debug_path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"📝 原始响应已保存: {debug_path}")
    return _parse_json_response(content)


# ══════════════════════════════════════════════════════
# 公众号 HTML 渲染
# ══════════════════════════════════════════════════════

WECHAT_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>硅谷AI深度</title>
</head>
<body style="margin:0;padding:0;background-color:#F5EDE0;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;color:#3C2415;line-height:1.75;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F5EDE0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;background-color:#FFFDF8;margin:0 auto;">

<!-- 封面图 -->
<tr><td style="padding:0;">
  <img src="https://your-cdn.com/covers/yt_interview.jpg" alt="硅谷AI深度" width="600" height="333" style="display:block;width:100%;height:auto;border:0;">
</td></tr>

<!-- 标题线 -->
<tr><td style="padding:0 20px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td style="border-top:2px solid #C8A882;"></td></tr>
  </table>
</td></tr>

<!-- 刊头 + 标题 -->
<tr><td style="padding:18px 20px 14px 20px;text-align:center;">
  <h1 style="margin:0;font-family:'Songti SC','SimSun',serif;font-size:22px;font-weight:700;color:#5C3317;letter-spacing:1px;line-height:1.5;">
    {title}
  </h1>
  <p style="margin:8px 0 0 0;font-size:13px;color:#A68A6B;">
    {subtitle}
  </p>
</td></tr>

<!-- 双线装饰 -->
<tr><td style="padding:0 20px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td style="border-top:1px solid #C8A882;"></td></tr>
  <tr><td style="height:3px;"></td></tr>
  <tr><td style="border-top:3px solid #8B6914;"></td></tr>
  </table>
</td></tr>

<!-- 信息卡 -->
<tr><td style="padding:14px 20px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FFF7ED;border:1px solid #E8D5B7;border-radius:4px;">
  <tr><td style="padding:12px 16px;">
    <p style="margin:0;font-size:13px;color:#5C4A3A;">
      🎙️ <strong>{speaker}</strong>（{speaker_title}） · 📺 {source}
    </p>
  </td></tr>
  </table>
</td></tr>

<!-- 导语 -->
<tr><td style="padding:12px 20px 8px 20px;">
  <p style="margin:0;font-size:15px;color:#5C4A3A;line-height:1.9;text-align:justify;">
    {intro}
  </p>
</td></tr>

<!-- 核心观点 -->
{sections_html}

<!-- 分割线 -->
<tr><td style="padding:8px 20px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td style="border-top:1px dashed #E0D0B8;"></td></tr>
  </table>
</td></tr>

<!-- 编辑视角 -->
<tr><td style="padding:12px 20px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FFF9F0;border-left:4px solid #8B4513;border-radius:0 4px 4px 0;">
  <tr><td style="padding:14px 16px;">
    <p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:#8B4513;">💡 润之视角</p>
    <p style="margin:0;font-size:14px;color:#5C4A3A;line-height:1.8;">{editor_note}</p>
  </td></tr>
  </table>
</td></tr>

<!-- 页脚 -->
<tr><td style="padding:20px;text-align:center;">
  <p style="margin:0;font-size:12px;color:#A68A6B;">
    硅谷AI深度 · 每日14:00更新<br>
    © 2026 秋山梦团队
  </p>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def render_article_html(data: dict, intro_text: str) -> str:
    """渲染深度文章为公众号HTML"""

    # 构建核心观点 HTML
    sections_html = ""
    for i, sec in enumerate(data.get("sections", [])):
        heading = sec.get("heading", "")
        body = sec.get("body", "")

        sections_html += f"""
<!-- 观点 {i+1} -->
<tr><td style="padding:10px 20px 6px 20px;">
  <h2 style="margin:0;font-size:16px;font-weight:700;color:#5C3317;border-bottom:2px solid #E8D5B7;padding-bottom:6px;">
    {heading}
  </h2>
</td></tr>
<tr><td style="padding:4px 20px 14px 20px;">
  <p style="margin:0;font-size:15px;color:#5C4A3A;line-height:1.9;text-align:justify;">
    {_bold_text(body)}
  </p>
</td></tr>"""

    html = WECHAT_TEMPLATE.format(
        title=data.get("title", "硅谷AI深度"),
        subtitle=data.get("subtitle", ""),
        speaker=data.get("speaker", ""),
        speaker_title=data.get("speaker_title", ""),
        source=data.get("source", ""),
        intro=intro_text,
        sections_html=sections_html,
        editor_note=data.get("editor_note", ""),
    )
    return html


def _bold_text(text: str) -> str:
    """将 **text** 转为 <strong>text</strong>"""
    return re.sub(r'\*\*(.+?)\*\*', r'<strong style="color:#8B4513;">\1</strong>', text)


# ══════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════

def main():
    beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
    date_str = beijing_now.strftime("%Y-%m-%d")
    today_cn = beijing_now.strftime("%Y年%m月%d日")

    logger.info("=" * 50)
    logger.info(f"🎬 每日YouTube深度访谈专栏 | {today_cn} 14:00")
    logger.info("=" * 50)

    # ── 1. 抓取 ──
    all_videos = []
    tiers = [
        ("🥇 第一梯队", TIER1_INTERVIEW_CHANNELS),
        ("🥈 第二梯队", TIER2_TECH_TALKS),
        ("🥉 第三梯队", TIER3_OFFICIAL),
    ]

    selected_video = None
    for tier_name, channels in tiers:
        if selected_video:
            break
        logger.info(f"\n{tier_name}：扫描 {len(channels)} 个频道...")
        for name, handle in channels.items():
            if selected_video:
                break
            videos = fetch_channel_videos(name, handle, max_videos=MAX_VIDEOS_PER_CHANNEL)
            for v in videos:
                is_it, score = is_interview(v)
                logger.info(f"  {'✅' if is_it else '❌'} {v['title'][:50]}... (评分:{score}, 时长:{v['duration']//60}分)")
                if is_it:
                    selected_video = v
                    logger.info(f"\n🎯 选中: {v['channel_name']} — {v['title']}")
                    break

    if not selected_video:
        logger.warning("⚠️ 今日无符合条件的深度访谈，跳过推送")
        return 0

    # ── 2. DeepSeek 深度文章 ──
    article = call_deepseek_article(selected_video["subtitle_text"])

    # ── 3. 渲染 HTML ──
    speaker_name = article.get("speaker", "")
    intro_text = f"{speaker_name} 在 {selected_video['channel_name']} 的最新访谈中，分享了关于{article.get('title','AI')[:20]}的深度思考。以下是本次对话的核心观点。"
    html = render_article_html(article, intro_text)

    # 保存
    output_path = os.path.join(WORKDIR, f"output_yt_{date_str}.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"✅ HTML 已写入: {output_path}")

    # ── 4. 推送公众号 ──
    try:
        token = get_access_token()
        cover_id = upload_cover(token)
        digest = article.get("subtitle", "")[:54]

        draft_id = create_draft(
            token, html,
            title=article.get("title", f"硅谷AI深度 | {today_cn}"),
            thumb_media_id=cover_id,
            digest=digest,
        )
        if draft_id:
            logger.info(f"✅ 微信草稿已创建: {draft_id}")
            publish_draft(token, draft_id)
        else:
            logger.warning("⚠️ 草稿创建失败")
    except Exception as e:
        logger.warning(f"⚠️ 微信推送失败: {e}")

    logger.info("=" * 50)
    logger.info(f"🎉 {today_cn} 深度访谈专栏完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
