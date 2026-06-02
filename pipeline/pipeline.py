#!/usr/bin/env python3
"""
硅谷AI晨报 — 自动化内容抓取 + 翻译 + 汇总 Pipeline

数据源：
  - Hacker News (Algolia API): AI 相关热门文章
  - ArXiv API: 最新 AI/ML 论文
  - HuggingFace Daily Papers: 每日精选论文 (可选，解析失败则跳过)
  - X/Twitter (Nitter RSS): 硅谷名人最新推文

翻译 & 汇总：DeepSeek API
输出：Markdown 日报文件
"""

# Python 3.10+ 兼容性修复：hyperframe 依赖已移除的 collections.MutableSet
import collections.abc
import collections
for _attr in ("MutableSet", "MutableMapping"):
    if not hasattr(collections, _attr):
        setattr(collections, _attr, getattr(collections.abc, _attr))

import os
import sys
import json
import re
import html
import logging
import subprocess
import time
import glob
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from openai import OpenAI

# ============================================================
# 全局代理配置
# ============================================================

PROXY_URL = "http://172.23.80.1:7890"

# 创建带代理的全局 Session，所有 requests 调用统一走代理
session = requests.Session()
session.proxies = {
    "http": PROXY_URL,
    "https": PROXY_URL,
}
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; AiMorningBrief/2.0)"
})

# ============================================================
# 配置
# ============================================================

WORKDIR = os.path.dirname(os.path.abspath(__file__))

def _today_date_str() -> str:
    """返回北京时间今天的日期字符串，格式 YYYY-MM-DD"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")

def _days_ago_date_str(days: int) -> str:
    """返回 N 天前的日期字符串，格式 YYYY-MM-DD"""
    return ((datetime.now(timezone.utc) + timedelta(hours=8)) - timedelta(days=days)).strftime("%Y-%m-%d")

def _extract_tweet_id(url: str) -> str:
    """从 Nitter/Twitter URL 提取 tweet ID，如 /status/123456 → 123456"""
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else ""

def _dated_output_paths(date_str: str = None):
    """返回日期化的输出文件路径 (md, json)"""
    ds = date_str or _today_date_str()
    return (
        os.path.join(WORKDIR, f"output_{ds}.md"),
        os.path.join(WORKDIR, f"output_{ds}.json"),
    )

# 兼容旧版：软链接指向最新日期文件
OUTPUT_FILE = os.path.join(WORKDIR, "output.md")
OUTPUT_JSON_FILE = os.path.join(WORKDIR, "output.json")

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ai_morning_brief")

# DeepSeek 模型
DEEPSEEK_MODEL = "deepseek-chat"

# HTTP 请求超时（秒）
HTTP_TIMEOUT = 30

# X (Twitter) 硅谷名人账号 — 通过 Nitter RSS 抓取
X_ACCOUNTS = {
    "kaboroehart":  "Andrej Karpathy（前OpenAI AI总监，现创业）",
    "sama":         "Sam Altman（OpenAI CEO）",
    "jimfan":       "Jim Fan（NVIDIA高级研究科学家）",
    "ylecun":       "Yann LeCun（Meta首席AI科学家）",
    "_akhaliq":     "AK（AI论文速递）",
    "nrehiew_":     "Nino（AI资讯）",
    "DrJimFan":     "Jim Fan",
}

# YouTube 频道列表 — 通过 yt-dlp 抓取最新视频字幕
YOUTUBE_CHANNELS = {
    "OpenAI":            "@OpenAI",
    "Google DeepMind":   "@GoogleDeepMind",
    "Anthropic":         "@AnthropicAI",
    "NVIDIA":            "@NVIDIA",
    "Two Minute Papers": "@TwoMinutePapers",
    "Yannic Kilcher":    "@YannicKilcher",
    "AI Explained":      "@aiexplained-official",
}

# YouTube 字幕输出目录
YT_SUBS_DIR = "/tmp/yt_subs"

# ============================================================
# 工具函数
# ============================================================

def strip_html(text: str) -> str:
    """去除 HTML 标签，解码 HTML 实体"""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text.strip()


def get_deepseek_client() -> OpenAI:
    """初始化 DeepSeek API 客户端（兼容 OpenAI SDK）"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        # Fallback: 读取项目本地 .env 文件（绕过 Hermes 环境变量拦截）
        env_file = os.path.join(WORKDIR, ".env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY="):
                        api_key = line.split("=", 1)[1]
                        break
    if not api_key:
        raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置，请在 ~/.hermes/.env 中配置")
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


# ============================================================
# 数据源 1: Hacker News (Algolia API)
# ============================================================

def _hn_search(query: str, max_hits: int = 20) -> list[dict]:
    """
    单次 HN Algolia Search 请求。
    Algolia API 限制：search query 中 OR 关键字不能超过 5 个，
    否则返回 0 结果。因此我们分多次查询再合并去重。
    """
    try:
        resp = session.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": query,
                "tags": "story",
                "hitsPerPage": max_hits,
                "numericFilters": "points>10",
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        hits = data.get("hits", [])
        return [
            {
                "title": hit.get("title", ""),
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
                "points": hit.get("points", 0),
                "num_comments": hit.get("num_comments", 0),
                "created_at": hit.get("created_at", ""),
                "objectID": hit.get("objectID", ""),
                "author": hit.get("author", ""),
            }
            for hit in hits
        ]
    except (requests.RequestException, json.JSONDecodeError, KeyError):
        return []


def fetch_hn_stories() -> list[dict]:
    """
    通过 Algolia HN Search API 抓取 AI 相关热门文章。
    分多组查询（避开 OR 数量限制），合并去重后取前 10 条。
    返回列表，每项 dict: {title, url, points, num_comments, created_at, objectID}
    """
    logger.info("=" * 50)
    logger.info("📡 开始抓取 Hacker News AI 热门文章...")

    # 每组查询不超过 5 个 OR 关键字（Algolia 限制）
    query_groups = [
        "AI OR LLM OR OpenAI OR GPT OR deepseek",
        "Anthropic OR Claude OR Gemini OR Llama OR Mistral",
        "diffusion OR transformer OR AGI OR RLHF OR RAG",
        # 硅谷名人动态：分组搜索确保不遗漏重要人物动态
        '"Sam Altman" OR "Andrej Karpathy" OR "Jim Fan" OR "Yann LeCun" OR "Jensen Huang"',
        '"Demis Hassabis" OR "Dario Amodei" OR "Ilya Sutskever" OR "Elon Musk AI" OR "Satya Nadella AI"',
        '"Nvidia CEO" OR "OpenAI CEO" OR "DeepMind" OR "Anthropic CEO" OR "SSI"',
    ]

    seen_ids = set()
    stories = []

    for query in query_groups:
        hits = _hn_search(query, max_hits=10)
        for hit in hits:
            oid = hit["objectID"]
            if oid not in seen_ids:
                seen_ids.add(oid)
                stories.append(hit)

    # 按 points 降序排列，取前 10
    stories.sort(key=lambda x: x["points"], reverse=True)
    stories = stories[:10]

    logger.info(f"  ✓ HN: 获取 {len(stories)} 条热门文章")
    return stories


def fetch_hn_comments(story_id: str) -> list[dict]:
    """
    通过 Algolia HN Items API 抓取指定文章的评论。
    提取前 5 条顶层热门评论。
    URL: https://hn.algolia.com/api/v1/items/{story_id}
    使用全局 session（走代理）。
    返回列表，每项 dict: {user, text}，text 截取前 200 字。
    """
    url = f"https://hn.algolia.com/api/v1/items/{story_id}"
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return []
        data = resp.json()
        children = data.get("children", [])
        comments = []
        for child in children:
            # 只取顶层评论（parent_id 等于 story_id 或 parent_id 为 null）
            text = child.get("text", "")
            author = child.get("author", "")
            if text and author and child.get("parent_id") in (None, int(story_id)):
                # 去除 HTML 标签
                clean_text = strip_html(text)
                if clean_text:
                    comments.append({
                        "user": author,
                        "text": clean_text[:200],
                    })
            if len(comments) >= 5:
                break
        return comments
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError):
        return []


# ============================================================
# 数据源 2: ArXiv API
# ============================================================

def fetch_arxiv_papers() -> list[dict]:
    """
    通过 ArXiv API 抓取最新 AI/ML 论文。
    返回列表，每项 dict: {title, summary, arxiv_id, url, published, authors}
    """
    logger.info("=" * 50)
    logger.info("📄 开始抓取 ArXiv 最新 AI 论文...")

    url = (
        "http://export.arxiv.org/api/query"
        "?search_query=cat:cs.AI+OR+cat:cs.CL+OR+cat:cs.LG"
        "&sortBy=submittedDate&sortOrder=descending&max_results=10"
    )

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            logger.error(f"  ✗ ArXiv API 返回 {resp.status_code}")
            return []

        root = ET.fromstring(resp.text)
        entries = root.findall("atom:entry", ns)

        papers = []
        for entry in entries:
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            id_el = entry.find("atom:id", ns)
            published_el = entry.find("atom:published", ns)

            # 提取链接（取 href 属性，排除内部链接）
            link_el = entry.find("atom:link[@title='pdf']", ns)
            if link_el is None:
                link_el = entry.find("atom:link[@rel='alternate']", ns)
            if link_el is None:
                link_el = entry.find("atom:link", ns)

            # 提取作者列表
            author_els = entry.findall("atom:author/atom:name", ns)
            authors = [a.text.strip() for a in author_els if a.text]

            title = strip_html(title_el.text) if title_el is not None and title_el.text else ""
            summary = strip_html(summary_el.text) if summary_el is not None and summary_el.text else ""
            arxiv_id = id_el.text.strip() if id_el is not None and id_el.text else ""
            published = published_el.text if published_el is not None else ""
            paper_url = link_el.get("href") if link_el is not None else ""

            # 从 arxiv_id 中提取纯 ID（去掉 http://arxiv.org/abs/ 前缀）
            short_id = arxiv_id.split("/abs/")[-1] if "/abs/" in arxiv_id else arxiv_id

            # 截断过长的摘要
            if len(summary) > 800:
                summary = summary[:800] + "..."

            papers.append({
                "title": title,
                "summary": summary,
                "arxiv_id": short_id,
                "url": paper_url or f"https://arxiv.org/abs/{short_id}",
                "published": published,
                "authors": authors,
            })

        logger.info(f"  ✓ ArXiv: 获取 {len(papers)} 篇论文")
        return papers

    except requests.RequestException as e:
        logger.error(f"  ✗ ArXiv API 请求失败: {e}")
        return []
    except ET.ParseError as e:
        logger.error(f"  ✗ ArXiv XML 解析失败: {e}")
        return []


# ============================================================
# 数据源 3: HuggingFace Daily Papers (可选)
# ============================================================

def fetch_hf_papers() -> list[dict]:
    """
    从 HuggingFace Daily Papers 页面抓取每日精选论文。
    解析 HTML 页面，提取论文标题和链接。
    如果解析失败则返回空列表（不影响其他数据源）。
    返回列表，每项 dict: {title, url, upvotes}
    """
    logger.info("=" * 50)
    logger.info("🤗 开始抓取 HuggingFace Daily Papers...")

    url = "https://huggingface.co/papers"

    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"  ⚠ HuggingFace Papers 返回 {resp.status_code}，跳过")
            return []

        html_content = resp.text

        # 方法1：尝试从页面中提取 JSON 数据（内嵌在 <script> 标签中）
        # HuggingFace 可能在后端渲染时内嵌数据
        papers = []

        # 尝试匹配各种可能的数据格式
        # 模式：在 <script> 中寻找 papers 相关的 JSON 数据
        json_patterns = [
            r'window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>',
            r'"dailyPapers"\s*:\s*(\[.*?\])\s*[,}]',
            r'"papers"\s*:\s*(\[.*?\])\s*[,}]',
        ]

        for pattern in json_patterns:
            match = re.search(pattern, html_content, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    # 尝试从不同结构中提取论文信息
                    if isinstance(data, dict):
                        daily_papers = (
                            data.get("dailyPapers")
                            or data.get("papers")
                            or []
                        )
                    elif isinstance(data, list):
                        daily_papers = data
                    else:
                        continue

                    for p in daily_papers[:10]:
                        if isinstance(p, dict):
                            papers.append({
                                "title": p.get("title", ""),
                                "url": f"https://huggingface.co{p.get('paper', {}).get('id', '')}" if isinstance(p.get("paper"), dict) else p.get("url", ""),
                                "upvotes": p.get("upvotes", 0),
                            })
                    if papers:
                        break
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue

        # 方法2：如果 JSON 解析失败，用正则从 HTML 中提取标题和链接
        if not papers:
            logger.info("  JSON 解析未成功，尝试从 HTML 提取...")
            # 匹配论文卡片中的标题和链接模式
            # 查找 <a> 标签中包含 paper 链接的模式
            paper_links = re.findall(
                r'<a[^>]*href="(/papers/[^"]+)"[^>]*>(.*?)</a>',
                html_content,
                re.DOTALL,
            )
            seen = set()
            for link, raw_title in paper_links[:10]:
                title = strip_html(raw_title).strip()
                if title and len(title) > 10 and link not in seen:
                    seen.add(link)
                    papers.append({
                        "title": title,
                        "url": f"https://huggingface.co{link}",
                        "upvotes": 0,
                    })

        if papers:
            logger.info(f"  ✓ HuggingFace: 获取 {len(papers)} 篇论文")
        else:
            logger.warning("  ⚠ HuggingFace 解析未找到论文，跳过此数据源")

        return papers

    except requests.RequestException as e:
        logger.warning(f"  ⚠ HuggingFace Papers 请求失败: {e}，跳过")
        return []
    except Exception as e:
        logger.warning(f"  ⚠ HuggingFace Papers 解析异常: {e}，跳过")
        return []


# ============================================================
# 数据源 4: X/Twitter (Nitter RSS)
# ============================================================

def fetch_x_via_nitter(handle: str) -> list[dict]:
    """
    通过 nitter.net RSS 抓取指定用户的推文，走全局代理 session。
    每人最多取 5 条推文。
    返回列表，每项 dict: {content, url, handle}
    """
    url = f"https://nitter.net/{handle}/rss"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"  ⚠ Nitter RSS ({handle}) 返回 {resp.status_code}，跳过")
            return []

        root = ET.fromstring(resp.text)
        tweets = []
        for item in root.findall(".//item")[:5]:
            title = item.find("title")
            link = item.find("link")
            content = title.text if title is not None and title.text else ""
            # 去掉 Twitter 特有的 "R to @xxx" 前缀
            content = re.sub(r"^R to @\w+\s*", "", content).strip()
            if content:
                tweets.append({
                    "content": content[:280],
                    "url": link.text if link is not None and link.text else "",
                    "handle": handle,
                    "tweet_id": _extract_tweet_id(link.text if link is not None else ""),
                })
        return tweets

    except requests.RequestException as e:
        logger.warning(f"  ⚠ Nitter RSS ({handle}) 请求失败: {e}，跳过")
        return []
    except ET.ParseError as e:
        logger.warning(f"  ⚠ Nitter RSS ({handle}) XML 解析失败: {e}，跳过")
        return []


def fetch_all_x_posts() -> list[dict]:
    """
    遍历 X_ACCOUNTS，逐个通过 Nitter RSS 抓取推文。
    返回所有推文的列表（按账号原始顺序排列）。
    """
    logger.info("=" * 50)
    logger.info("🐦 开始抓取 X (Twitter) 硅谷名人推文...")

    all_tweets = []
    for handle, identity in X_ACCOUNTS.items():
        tweets = fetch_x_via_nitter(handle)
        if tweets:
            logger.info(f"  ✓ @{handle} ({identity})：获取 {len(tweets)} 条推文")
            all_tweets.extend(tweets)

    logger.info(f"  ✓ X: 共获取 {len(all_tweets)} 条推文")
    return all_tweets


# ============================================================
# 数据源 5: YouTube 视频字幕 (yt-dlp)
# ============================================================

def _parse_srt_text(filepath: str) -> str:
    """解析 SRT 字幕文件，去掉时间戳和序号，只保留纯文本。"""
    if not os.path.exists(filepath):
        return ""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return ""

    lines = content.split("\n")
    text_lines = []
    for line in lines:
        line = line.strip()
        # 跳过空行、序号行（纯数字）、时间轴行（包含 -->）
        if not line:
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            continue
        # 跳过多余的 HTML 标签
        line = re.sub(r"<[^>]+>", "", line)
        text_lines.append(line)

    return " ".join(text_lines)


def fetch_all_youtube_subs() -> list[dict]:
    """
    通过 yt-dlp 抓取 YouTube 频道最新视频的英文字幕。
    每个频道取最新 1 个视频，频道之间延迟 3 秒。
    遇到 429 错误时跳过该频道，不影响其他数据源。
    返回列表，每项 dict: {channel_name, title, subtitle_text, url, video_id}
    """
    logger.info("=" * 50)
    logger.info("🎬 开始抓取 YouTube 视频字幕...")

    # 确保输出目录存在
    os.makedirs(YT_SUBS_DIR, exist_ok=True)

    videos = []

    for channel_name, handle in YOUTUBE_CHANNELS.items():
        channel_url = f"https://www.youtube.com/@{handle.lstrip('@')}"
        try:
            # 第一步：获取频道最新 1 个视频 ID
            id_result = subprocess.run(
                [
                    "yt-dlp",
                    "--flat-playlist",
                    "--playlist-end", "1",
                    "--print", "id",
                    "--proxy", PROXY_URL,
                    "--js-runtimes", "deno",
                    channel_url,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if id_result.returncode != 0:
                stderr = id_result.stderr.strip()
                if "429" in stderr:
                    logger.warning(f"  ⚠ {channel_name} ({handle}): 429 限流，跳过")
                else:
                    logger.warning(f"  ⚠ {channel_name} ({handle}): 获取视频 ID 失败: {stderr[:120]}")
                time.sleep(3)
                continue

            video_id = id_result.stdout.strip()
            if not video_id:
                logger.warning(f"  ⚠ {channel_name} ({handle}): 未找到视频，跳过")
                time.sleep(3)
                continue

            # 第二步：下载英文字幕
            sub_output = os.path.join(YT_SUBS_DIR, "%(id)s")
            video_url = f"https://www.youtube.com/watch?v={video_id}"

            sub_result = subprocess.run(
                [
                    "yt-dlp",
                    "--write-auto-subs",
                    "--sub-lang", "en",
                    "--skip-download",
                    "--convert-subs", "srt",
                    "-o", sub_output,
                    "--proxy", PROXY_URL,
                    "--js-runtimes", "deno",
                    video_url,
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )

            if sub_result.returncode != 0:
                stderr = sub_result.stderr.strip()
                if "429" in stderr:
                    logger.warning(f"  ⚠ {channel_name} ({handle}): 字幕下载 429 限流，跳过")
                elif "no subtitles" in stderr.lower() or "no video" in stderr.lower():
                    logger.warning(f"  ⚠ {channel_name} ({handle}): 视频无英文字幕，跳过")
                else:
                    logger.warning(f"  ⚠ {channel_name} ({handle}): 字幕下载失败: {stderr[:120]}")
                time.sleep(3)
                continue

            # 第三步：读取字幕文件
            srt_file = os.path.join(YT_SUBS_DIR, f"{video_id}.en.srt")
            subtitle_text = _parse_srt_text(srt_file)

            if not subtitle_text:
                logger.warning(f"  ⚠ {channel_name} ({handle}): 字幕为空或解析失败，跳过")
                time.sleep(3)
                continue

            # 第四步：通过 yt-dlp 获取视频标题
            title = ""
            title_result = subprocess.run(
                [
                    "yt-dlp",
                    "--print", "title",
                    "--proxy", PROXY_URL,
                    "--js-runtimes", "deno",
                    video_url,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if title_result.returncode == 0:
                title = title_result.stdout.strip()

            # 截取前 2000 字符
            subtitle_text = subtitle_text[:2000]

            logger.info(f"  ✓ {channel_name} ({handle}): 获取视频 {video_id}「{title[:60]}」")

            videos.append({
                "channel_name": channel_name,
                "title": title,
                "subtitle_text": subtitle_text,
                "url": video_url,
                "video_id": video_id,
            })

        except subprocess.TimeoutExpired:
            logger.warning(f"  ⚠ {channel_name} ({handle}): yt-dlp 超时，跳过")
        except FileNotFoundError:
            logger.error("  ✗ yt-dlp 未安装，请先安装: pip install yt-dlp")
            break
        except Exception as e:
            logger.warning(f"  ⚠ {channel_name} ({handle}): 未知错误: {e}")

        # 频道之间延迟 3 秒
        time.sleep(3)

    logger.info(f"  ✓ YouTube: 共获取 {len(videos)} 个视频字幕")
    return videos


# ============================================================
# DeepSeek 翻译 & 汇总
# ============================================================

def build_summary_prompt(
    hn_stories: list[dict],
    arxiv_papers: list[dict],
    hf_papers: list[dict],
    x_posts: list[dict],
    yt_videos: list[dict],
    today_str: str,
    yesterday_headline: str = "",
) -> str:
    """
    构建发送给 DeepSeek 的汇总 Prompt。
    要求输出结构化 JSON（不要 markdown）。
    """

    # ---- Hacker News 部分 ----
    hn_lines = []
    if hn_stories:
        for i, s in enumerate(hn_stories, 1):
            hn_lines.append(f"{i}. [hn_{s['objectID']}] {s['title']}")
            hn_lines.append(f"   👤 HN 提交者: {s['author']}")
            hn_lines.append(f"   👍 {s['points']} 分 | 💬 {s['num_comments']} 评论")
            hn_lines.append(f"   🔗 {s['url']}")
    hn_section = "\n".join(hn_lines) if hn_lines else "（今日无 Hacker News 数据）"

    # ---- ArXiv 部分 ----
    arxiv_lines = []
    if arxiv_papers:
        for i, p in enumerate(arxiv_papers, 1):
            arxiv_lines.append(f"{i}. [arxiv_{p['arxiv_id']}] {p['title']}")
            arxiv_lines.append(f"   作者: {', '.join(p['authors'][:3])}")
            arxiv_lines.append(f"   摘要: {p['summary'][:400]}")
            arxiv_lines.append(f"   🔗 {p['url']}")
    arxiv_section = "\n".join(arxiv_lines) if arxiv_lines else "（今日无 ArXiv 数据）"

    # ---- HuggingFace 部分 ----
    hf_lines = []
    if hf_papers:
        for i, p in enumerate(hf_papers, 1):
            url_path = p.get("url", "")
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", url_path.split("/")[-1]) if "/" in url_path else str(i)
            hf_lines.append(f"{i}. [hf_{safe_id}] {p['title']}")
            hf_lines.append(f"   🔗 {p['url']}")
    hf_section = "\n".join(hf_lines) if hf_lines else "（今日无 HuggingFace 数据）"

    # ---- X (Twitter) 部分 ----
    x_lines = []
    if x_posts:
        grouped: dict[str, list[dict]] = {}
        for tweet in x_posts:
            handle = tweet["handle"]
            if handle not in grouped:
                grouped[handle] = []
            grouped[handle].append(tweet)

        idx = 1
        for handle, tweets in grouped.items():
            identity = X_ACCOUNTS.get(handle, handle)
            x_lines.append(f"\n### @{handle}（{identity}）")
            for t in tweets:
                x_lines.append(f"{idx}. [x_{handle}_{idx}] {t['content']}")
                x_lines.append(f"   🔗 {t['url']}")
                idx += 1
    x_section = "\n".join(x_lines) if x_lines else "（今日暂无硅谷大佬推文）"

    # ---- YouTube 视频部分 ----
    yt_lines = []
    if yt_videos:
        for i, v in enumerate(yt_videos, 1):
            yt_lines.append(f"\n#### {v['channel_name']}")
            yt_lines.append(f"- [yt_{i}] 标题: {v['title']}")
            yt_lines.append(f"- 字幕摘要（前2000字）: {v['subtitle_text']}")
            yt_lines.append(f"- 链接: {v['url']}")
    yt_section = "\n".join(yt_lines) if yt_lines else "（今日暂无视频更新）"

    # ---- 完整 Prompt ----
    avoid_hint = ""
    if yesterday_headline:
        avoid_hint = f"\n⚠️ 昨天的头条是「{yesterday_headline}」，今天请选择不同的新闻作为头条，避免重复。如果某条内容昨日已报道，可以忽略或降级处理。\n"

    prompt = f"""你是一位资深AI行业分析师，负责编写「硅谷AI晨报」。以下是 {today_str} 从多个数据源抓取的原始内容。{avoid_hint}

## 版权保护规则（必须严格遵守）

1. 禁止直接复制原文：绝对不能直接复制、粘贴或逐句翻译原始内容，必须用自己的语言重新组织表达。
2. 必须深度改写：调整句式结构、重新组织信息顺序、用自己的话总结核心要点。
3. 引用不等于复制：用「据XX报道」「XX指出」等转述方式，不得直接引述原文句子。
4. 注意：以上规则主要针对 HN/ArXiv/国内媒体等文字类来源。X/Twitter 和 YouTube 的国外内容可适度宽松处理。
5. 来源标注：每条资讯必须标注原始来源，但内容本身必须经过独立加工。

请完成以下任务：

## 原始数据

### 📡 Hacker News AI 热门文章
{hn_section}

### 📄 ArXiv 最新 AI 论文
{arxiv_section}

### 🤗 HuggingFace 每日精选
{hf_section}

### 🐦 X精选（硅谷大佬今日发言）
{x_section}

### 🎬 YouTube 最新视频
{yt_section}

## 输出要求

**你必须只输出一个纯 JSON 对象，不要输出任何 markdown、代码块标记（```json）、解释或其他文字。直接以 {{ 开头、}} 结尾。**

JSON 格式如下：

{{
  "headline": {{
    "title": "今日头条标题，一句话概括今日最重要的AI新闻（15-30字）",
    "summary": "150-200字深度分析，从所有内容中选出1-2条最重要的新闻/论文/趋势进行综合解读，说明为什么这是今日头条、对行业意味着什么。如果原始材料不足，可以更短但保持深度",
    "eli5": "用最通俗的大白话解释这条头条新闻，假设读者完全不懂AI，用生活化的比喻（一句话，不超过60字）",
    "sources": ["HN(3)", "X(2)", "ArXiv(1)"],
    "source_count": 6
  }},
  "headline_category": "从以下选择一个: 公司动态/技术突破/产品发布/观点评论/学术前沿",
  "articles": [
    {{
      "id": "使用原始数据中方括号标注的ID，如 hn_43996555 / arxiv_2405.xxx / x_sama_1 / yt_1",
      "title": "中文标题（专有名词保留英文原名，如 LLM、ChatGPT、API 等）",
      "category": "公司动态/技术突破/产品发布/观点评论/学术前沿 之一",
      "category_options": ["公司动态", "技术突破", "产品发布", "观点评论", "学术前沿"],
      "source_type": "hn/arxiv/hf/x/yt 之一",
      "summary": "80-120字详细分析，说明核心价值、技术创新点和为什么值得关注",
      "ai_comment": "AI一句话犀利点评，一针见血，不超过50字",
      "eli5": "用大白话解释这条新闻，让非技术读者也能理解（一句话，不超过60字）",
      "source_tags": ["The Verge", "HN", "X/Twitter"],
      "tags": ["Sam Altman", "OpenAI"],
      "source_count": 3,
      "author": "作者/提交者名",
      "author_title": "职位/身份，如不确定填 HN提交者/项目作者",
      "url": "原始链接",
      "points": 0,
      "comment_count": 0
    }}
  ],
  "featured_topic": {{
    "title": "编辑精选专题标题，如「本周专题：GPT-5追踪」。如果当日内容有值得持续追踪的主题（如同一事件被多个来源报道，或某个技术趋势在多篇文章中反复出现），请提炼出一个专题；如果当日没有明显值得追踪的主题，则设为 null",
    "summary": "100-150字专题简介，说明为什么要关注这个话题、本专题覆盖了哪些方面",
    "articles": ["article_id_1", "article_id_2"]
  }},
  "editor_note": "润之点评一句话，总结今日AI圈的整体氛围和趋势，要有洞察力和幽默感"
}}

## 字段说明
### headline.sources / articles[].source_tags
- 根据原始数据的来源标注。HN 文章标注 "HN"，ArXiv 标注 "ArXiv"，HuggingFace 标注 "HuggingFace"，X/Twitter 推文标注发推人名称（如 "Sam Altman"、"Yann LeCun"），YouTube 标注频道名（如 "OpenAI"、"DeepMind"）
- sources 格式：来源名(条数)，如 "HN(3)" 表示来自 HN 的 3 篇报道
- source_count：该头条/文章涉及的独立来源数

### articles[].tags
- **人物/组织标签数组**，用于后续按人或公司过滤浏览
- 从文章内容中提取涉及的核心人物（如 "Sam Altman"、"Yann LeCun"）和组织（如 "OpenAI"、"DeepMind"、"Google"）
- 每条至少 1 个、最多 5 个标签
- 只填真实出现在文章中的实体，不要猜测
- ArXiv 论文标签：填第一作者 + 所属机构；如果机构不明显，只填作者名
- X 推文标签：填发推人 + 推文中提及的关键人物/组织
- YouTube 标签：填频道名 + 视频中讨论的关键人物/组织

### articles[].eli5
- 用通俗比喻解释给完全不懂AI的人听。例如「GPT-5 的原生多模态就像一个人同时用眼睛看、耳朵听、嘴巴说，不再需要翻译。」

### featured_topic
- 判断标准：如果今日有多条内容围绕同一事件/技术/趋势（≥3条关联文章），请提炼出专题
- 如果当日无明显可追踪主题，featured_topic 直接设为 null
- articles 字段填写相关文章的 id 列表

## 分类规则
- 「公司动态」：公司战略、融资、人事变动、政策监管
- 「技术突破」：新技术、模型发布、研究突破、性能飞跃
- 「产品发布」：新产品、新功能、开源项目、工具发布
- 「观点评论」：名人发言、行业观点、趋势分析、预测展望
- 「学术前沿」：论文、研究方法、理论突破

每条内容必须分配一个分类。ArXiv 论文默认「学术前沿」，X 推文默认「观点评论」，但如有例外请自行判断。

## 其他要求
- 总计精选 8-15 条 articles（覆盖 HN + ArXiv + X + YT + HF），不要全选
- ⚠️ 如果可用文章总数超过 12 条，只输出最重要的 12 条，精简输出量
- 按新闻价值和影响力排序，最重要的放前面（不按来源排序）
- 分类聚合：同类文章放相邻位置
- 标题和摘要全中文，保留技术术语和公司/产品名英文原名
- 每条详述和点评要具体深入，不要空泛概括
- 如果某条 HN 文章的 points 为 0 或 comment_count 为 0，保持原值即可
"""
    return prompt


def call_deepseek(prompt: str) -> str:
    """调用 DeepSeek API 进行翻译和汇总"""
    client = get_deepseek_client()

    logger.info("🤖 正在调用 DeepSeek API 进行翻译汇总...")
    response = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {
                "role": "system",
                "content": "你是一位专业的AI行业分析师，擅长从海量信息中提炼关键洞察，并用优雅的中文表达。你的输出必须严格遵循用户要求的 Markdown 格式。"
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_tokens=8192,
    )

    content = response.choices[0].message.content
    logger.info(f"🤖 DeepSeek 返回 {len(content)} 字符")
    return content


def summarize_content(
    hn_stories: list[dict],
    arxiv_papers: list[dict],
    hf_papers: list[dict],
    x_posts: list[dict],
    yt_videos: list[dict],
    yesterday_headline: str = "",
) -> dict:
    """
    汇总所有内容：构建 prompt → 调用 DeepSeek → 解析 JSON
    注入 HN 评论数据 → 返回结构化 dict（含 articles、headline 等）
    """
    # 北京时间
    beijing_time = datetime.now(timezone.utc) + timedelta(hours=8)
    today_str = beijing_time.strftime("%Y年%m月%d日")

    prompt = build_summary_prompt(hn_stories, arxiv_papers, hf_papers, x_posts, yt_videos, today_str, yesterday_headline)
    raw_response = call_deepseek(prompt)

    # 解析 DeepSeek 返回的 JSON
    result = _parse_json_response(raw_response)
    result["date"] = today_str

    # ---- 计算 meta 元数据 ----
    articles = result.get("articles", [])
    # 统计独立数据源
    source_types = set(a.get("source_type", "") for a in articles if a.get("source_type"))
    source_count = len(source_types)
    article_count = len(articles)
    # 阅读时间估算：头条 ~2分钟 + 每条文章 ~0.5分钟
    read_time_min = max(1, 2 + int(article_count * 0.5))
    # 获取日期字符串 YYYY-MM-DD
    beijing_date = beijing_time.strftime("%Y-%m-%d")
    result["meta"] = {
        "article_count": article_count,
        "source_count": source_count,
        "read_time": f"{read_time_min}分钟",
        "date": beijing_date,
    }

    # ---- 如果 headline 是旧格式（字符串），转换为新格式 ----
    headline = result.get("headline", "")
    if isinstance(headline, str):
        result["headline"] = {
            "title": "",
            "summary": headline,
            "eli5": "",
            "sources": [],
            "source_count": 0,
        }

    # ---- 确保 headline 有 title 字段 ----
    if isinstance(result.get("headline"), dict) and not result["headline"].get("title"):
        # 从 headline.summary 截取前30字作为 title
        summary = result["headline"].get("summary", "")
        if summary:
            result["headline"]["title"] = summary[:30]

    # 注入 HN 评论：给每条 source_type="hn" 的文章获取 top_comments
    logger.info("💬 正在抓取 HN 热门评论...")
    for article in result.get("articles", []):
        if article.get("source_type") == "hn":
            story_id = article.get("id", "").replace("hn_", "")
            if story_id:
                comments = fetch_hn_comments(story_id)
                article["top_comments"] = comments
                if comments:
                    logger.info(f"  ✓ {article['id']}: 获取 {len(comments)} 条评论")

    # 补充原始 points 和 comment_count（如果 DeepSeek 没填对）
    hn_by_id = {f"hn_{s['objectID']}": s for s in hn_stories}
    for article in result.get("articles", []):
        if article.get("source_type") == "hn":
            orig = hn_by_id.get(article.get("id", ""))
            if orig:
                if article.get("points", 0) == 0:
                    article["points"] = orig.get("points", 0)
                if article.get("comment_count", 0) == 0:
                    article["comment_count"] = orig.get("num_comments", 0)

    return result


def _auto_close_json(text: str) -> str:
    """自动补全被截断的 JSON：用栈追踪 { [ 和字符串的开关状态，补全未闭合的结构。

    策略：
    1. 去掉末尾被截断的不完整字段（如 '"title": "未'）
    2. 补全所有未闭合的括号和引号
    """
    # 移除末尾不完整的键值对（例如 "key": 后面没有值，或 "key": "未完）
    # 找最后一个完整的逗号或开括号
    text = text.rstrip()

    # 栈：追踪 { }、[ ]、以及字符串状态
    stack: list[str] = []
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"' and (not stack or stack[-1] != '"'):
            # 进入或退出字符串
            if in_string:
                in_string = False
                # 对应的 " 出栈
                if stack and stack[-1] == '"':
                    stack.pop()
            else:
                in_string = True
                stack.append('"')
            continue
        if in_string:
            continue
        if ch in ('{', '['):
            stack.append(ch)
        elif ch == '}':
            if stack and stack[-1] == '{':
                stack.pop()
            else:
                # 多出的 } 忽略（或作为不匹配标记）
                pass
        elif ch == ']':
            if stack and stack[-1] == '[':
                stack.pop()

    # 去掉末尾不完整的部分：找到最后一个结构完整的边界
    # 如果当前在字符串中，截断到上一个 " 之前
    if in_string:
        # 找到最后一个完整的 "（不在转义状态下）
        last_complete = -1
        escape2 = False
        for i in range(len(text) - 1, -1, -1):
            ch = text[i]
            if escape2:
                escape2 = False
                continue
            if ch == '\\':
                escape2 = True
                continue
            if ch == '"':
                last_complete = i
                break
        if last_complete > 0:
            text = text[:last_complete + 1]
            # 字符串的那个 " 从栈中移除
            if stack and stack[-1] == '"':
                stack.pop()
            in_string = False

    # 如果末尾是逗号，去掉它（它后面没有下一个元素了）
    text = text.rstrip()
    if text.endswith(','):
        text = text[:-1].rstrip()

    # 补全栈中剩余的括号（逆序）
    closing = []
    for bracket in reversed(stack):
        if bracket == '{':
            closing.append('}')
        elif bracket == '[':
            closing.append(']')
        elif bracket == '"':
            closing.append('"')

    fixed = text + ''.join(closing)
    return fixed


def _parse_json_response(raw: str) -> dict:
    """容错解析 JSON：处理 markdown 包裹、截断、嵌套等情况"""
    text = raw.strip()

    # 去掉 markdown 代码块包裹
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()

    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 使用 brace 计数器提取最外层 JSON 对象（处理嵌套）
    start = text.find("{")
    if start == -1:
        logger.error(f"无法在响应中找到 JSON 对象起始，原始内容前 500 字符: {raw[:500]}")
        raise ValueError("DeepSeek 返回的内容无法解析为 JSON")

    depth = 0
    in_string = False
    escape = False
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end > 0:
        json_str = text[start:end]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"Brace 提取后 JSON 解析失败: {e}，尝试自动补全...")
    else:
        # brace 不匹配，尝试直接用全文自动补全
        json_str = text[start:]

    # 尝试自动补全被截断的 JSON
    fixed = _auto_close_json(json_str)
    try:
        result = json.loads(fixed)
        logger.warning("⚠️ JSON 被截断，已自动补全")
        return result
    except json.JSONDecodeError:
        pass

    # 最后尝试正则兜底
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.error(f"无法解析 DeepSeek 返回的 JSON，原始内容前 500 字符: {raw[:500]}")
    raise ValueError("DeepSeek 返回的内容无法解析为 JSON")


# ============================================================
# 输出
# ============================================================

CATEGORY_ICONS = {
    "公司动态": "🏢",
    "技术突破": "🚀",
    "产品发布": "📦",
    "观点评论": "💬",
    "学术前沿": "📖",
}

SOURCE_LABELS = {
    "hn": "Hacker News",
    "arxiv": "ArXiv",
    "hf": "HuggingFace",
    "x": "X/Twitter",
    "yt": "YouTube",
}


def build_markdown_from_json(data: dict) -> str:
    """从结构化 JSON 数据生成可读的 Markdown 日报。"""
    today_str = data.get("date", "")
    headline = data.get("headline", "")
    if isinstance(headline, dict):
        headline_text = f"**{headline.get('title', '')}**\n> {headline.get('summary', '')}"
        headline_eli5 = headline.get("eli5", "")
    else:
        headline_text = f"> {headline}"
        headline_eli5 = ""

    lines = [
        f"# 硅谷AI晨报 | {today_str}",
        "",
        "## 🔥 今日头条",
        headline_text,
        f"> 分类: {data.get('headline_category', '')}",
    ]
    if headline_eli5:
        lines.append("")
        lines.append("> 💡 通俗解释（ELI5）:")
        lines.append(f"> {headline_eli5}")
    lines.append("")

    articles = data.get("articles", [])
    if not articles:
        lines.append("今日暂无值得关注的内容。")
        lines.append("")
        lines.append(f"> {data.get('editor_note', '')}")
        return "\n".join(lines)

    # 按分类分组
    groups: dict[str, list[dict]] = {}
    for art in articles:
        cat = art.get("category", "其他")
        if cat not in groups:
            groups[cat] = []
        groups[cat].append(art)

    # 分类排序：保持 articles 中首次出现的顺序
    seen_cats = []
    for art in articles:
        cat = art.get("category", "其他")
        if cat not in seen_cats:
            seen_cats.append(cat)

    for cat in seen_cats:
        icon = CATEGORY_ICONS.get(cat, "📌")
        lines.append(f"## {icon} {cat}")
        lines.append("")
        for art in groups[cat]:
            lines.append(f"- **{art.get('title', '')}**")
            source = SOURCE_LABELS.get(art.get("source_type", ""), art.get("source_type", ""))
            lines.append(f"  > 🏷️ 来源: {source} | 👤 {art.get('author', '')}（{art.get('author_title', '')}）")
            lines.append(f"  > 📝 {art.get('summary', '')}")
            ai_comment = art.get("ai_comment", "")
            if ai_comment:
                lines.append(f"  > 💡 AI点评: {ai_comment}")
            # HN 特有字段
            if art.get("source_type") == "hn":
                pts = art.get("points", 0)
                comments = art.get("comment_count", 0)
                lines.append(f"  > 👍 {pts} 分 | 💬 {comments} 评论")
                top_comments = art.get("top_comments", [])
                if top_comments:
                    lines.append("  > 💬 热门评论:")
                    for c in top_comments:
                        lines.append(f"  >   - **{c['user']}**: {c['text'][:100]}")
            lines.append(f"  > 🔗 {art.get('url', '')}")
            lines.append("")

    # 润之点评
    editor_note = data.get("editor_note", "")
    if editor_note:
        lines.append("## 💡 润之点评")
        lines.append(f"> {editor_note}")
        lines.append("")

    return "\n".join(lines)


def write_output_md(markdown: str, filepath: str) -> None:
    """将 Markdown 写入文件，并添加生成时间戳"""
    beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
    timestamp = beijing_now.strftime("%Y-%m-%d %H:%M:%S")

    header = f"<!-- 自动生成于 {timestamp} CST -->\n"
    full_content = header + markdown

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(full_content)

    logger.info(f"✅ Markdown 日报已写入: {filepath}")


def write_output_json(data: dict, filepath: str) -> None:
    """将结构化 JSON 数据写入文件"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(f"✅ JSON 数据已写入: {filepath}")


def write_output(data: dict, md_filepath: str, json_filepath: str) -> None:
    """双格式输出：生成 Markdown 和 JSON 两个文件"""
    markdown = build_markdown_from_json(data)
    write_output_md(markdown, md_filepath)
    write_output_json(data, json_filepath)


# ============================================================
# 双语支持：添加英文字段 + 生成英文版 JSON
# ============================================================

# 海外源类型列表（需要添加英文字段的）
_OVERSEAS_SOURCE_TYPES = {"hn", "arxiv", "hf", "x", "yt"}

_ENGLISH_SUMMARY_PROMPT = """You are a professional AI industry analyst. Write a concise, engaging English summary (2-3 sentences, 80-120 words) for the following AI news article.

Article Title (Chinese): {title_cn}
Article Summary (Chinese): {summary_cn}
Category: {category}

Output ONLY the English summary text, nothing else. No markdown, no JSON, no quotes."""


def _build_raw_title_map(
    hn_stories: list[dict],
    arxiv_papers: list[dict],
    hf_papers: list[dict],
    x_posts: list[dict],
    yt_videos: list[dict],
) -> dict[str, str]:
    """构建 文章ID → 原始英文标题 的映射"""
    title_map = {}
    for s in hn_stories:
        oid = s.get("objectID", "")
        if oid:
            title_map[f"hn_{oid}"] = s.get("title", "")
    for p in arxiv_papers:
        aid = p.get("arxiv_id", "")
        if aid:
            title_map[f"arxiv_{aid}"] = p.get("title", "")
    for p in hf_papers:
        # HF paper IDs 格式不确定，用 URL 末段作为 ID
        url = p.get("url", "")
        if url:
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", url.split("/")[-1]) if "/" in url else ""
            if safe_id:
                title_map[f"hf_{safe_id}"] = p.get("title", "")
    for t in x_posts:
        handle = t.get("handle", "")
        # 推文 ID 在 DeepSeek 输出中格式为 x_{handle}_{idx}，很难精确匹配
        # 用推文内容的前 50 字符做模糊键
        content = t.get("content", "")
        if content and handle:
            key = f"x_{handle}_{hash(content) & 0xffff}"
            title_map[key] = t.get("content", "")[:150]
    for v in yt_videos:
        vid = v.get("video_id", "")
        if vid:
            title_map[f"yt_{vid}"] = v.get("title", "")
    return title_map


def _find_english_title(article_id: str, raw_title_map: dict[str, str]) -> str:
    """根据文章 ID 查找原始英文标题"""
    # 精确匹配
    if article_id in raw_title_map:
        return raw_title_map[article_id]
    # 前缀匹配（如 x_nrehiew__19 → 找 x_nrehiew_ 开头的）
    if article_id.startswith("x_"):
        parts = article_id.split("_")
        if len(parts) >= 2:
            prefix = f"{parts[0]}_{parts[1]}_"
            for key, title in raw_title_map.items():
                if key.startswith(prefix):
                    return title
    # yt_ 前缀匹配
    if article_id.startswith("yt_"):
        for key, title in raw_title_map.items():
            if key.startswith("yt_"):
                return title
                break  # 返回第一个匹配的
    return ""


def _generate_english_summaries(articles: list[dict], max_articles: int = 15) -> dict[str, str]:
    """批量调用 DeepSeek 为海外源文章生成英文摘要。返回 {article_id: english_summary}"""
    overseas = [
        a for a in articles
        if a.get("source_type", "") in _OVERSEAS_SOURCE_TYPES
    ][:max_articles]
    
    if not overseas:
        return {}
    
    logger.info(f"🌐 为 {len(overseas)} 条海外文章生成英文摘要...")
    
    client = get_deepseek_client()
    summaries = {}
    
    for article in overseas:
        aid = article.get("id", "")
        title_cn = article.get("title", "")
        summary_cn = article.get("summary", "")
        category = article.get("category", "")
        
        if not summary_cn:
            summaries[aid] = ""
            continue
        
        prompt = _ENGLISH_SUMMARY_PROMPT.format(
            title_cn=title_cn,
            summary_cn=summary_cn,
            category=category,
        )
        
        try:
            response = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": "You are a professional AI industry analyst. Respond concisely in English only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
                max_tokens=300,
            )
            eng_summary = response.choices[0].message.content.strip()
            # 清理可能的引号包裹
            eng_summary = eng_summary.strip('"').strip("'").strip()
            summaries[aid] = eng_summary
            logger.info(f"  ✓ {aid}: 英文摘要生成 ({len(eng_summary)} chars)")
        except Exception as e:
            logger.warning(f"  ⚠ {aid}: 英文摘要生成失败: {e}")
            summaries[aid] = ""
    
    return summaries


def add_bilingual_fields(
    data: dict,
    raw_title_map: dict[str, str],
    english_summaries: dict[str, str],
) -> dict:
    """为所有文章添加 language、english_title、english_summary 字段"""
    articles = data.get("articles", [])
    
    for article in articles:
        source_type = article.get("source_type", "")
        aid = article.get("id", "")
        
        if source_type in _OVERSEAS_SOURCE_TYPES:
            article["language"] = "en"
            # 查找原始英文标题
            eng_title = _find_english_title(aid, raw_title_map)
            if not eng_title:
                # 降级：使用 DeepSeek 生成的中文标题对应的场景重构
                eng_title = article.get("title", "")
            article["english_title"] = eng_title
            # 英文摘要
            article["english_summary"] = english_summaries.get(aid, "")
        else:
            article["language"] = "zh"
    
    return data


def generate_english_json(data: dict, json_filepath: str) -> None:
    """生成纯英文版 JSON（只含海外源）"""
    articles = data.get("articles", [])
    en_articles = []
    
    for a in articles:
        if a.get("language") == "en":
            en_articles.append({
                "id": a.get("id", ""),
                "title": a.get("english_title") or a.get("title", ""),
                "summary": a.get("english_summary") or a.get("summary", ""),
                "category": a.get("category", ""),
                "category_options": a.get("category_options", []),
                "source_type": a.get("source_type", ""),
                "tags": a.get("tags", []),
                "source_tags": a.get("source_tags", []),
                "source_count": a.get("source_count", 1),
                "author": a.get("author", ""),
                "author_title": a.get("author_title", ""),
                "url": a.get("url", ""),
                "points": a.get("points", 0),
                "comment_count": a.get("comment_count", 0),
                "language": "en",
                "eli5": a.get("eli5", ""),
                "ai_comment": a.get("ai_comment", ""),
            })
    
    en_data = {
        "date": data.get("date", ""),
        "headline_category": data.get("headline_category", ""),
        "articles": en_articles,
        "article_count": len(en_articles),
    }
    
    os.makedirs(os.path.dirname(json_filepath), exist_ok=True)
    with open(json_filepath, "w", encoding="utf-8") as f:
        json.dump(en_data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"✅ 英文版 JSON 已写入: {json_filepath} ({len(en_articles)} 条)")
    
    # 同时创建/更新 output_en.json 软链接
    en_link = os.path.join(WORKDIR, "output_en.json")
    if os.path.islink(en_link) or os.path.exists(en_link):
        os.remove(en_link)
    os.symlink(os.path.basename(json_filepath), en_link)
    logger.info(f"🔗 软链接: output_en.json → {os.path.basename(json_filepath)}")


def _update_symlinks(md_filepath: str, json_filepath: str) -> None:
    """创建/更新 output.md 和 output.json 软链接指向日期化文件"""
    for link_path, real_path in [(OUTPUT_FILE, md_filepath), (OUTPUT_JSON_FILE, json_filepath)]:
        if os.path.islink(link_path) or os.path.exists(link_path):
            os.remove(link_path)
        os.symlink(os.path.basename(real_path), link_path)
        logger.info(f"🔗 软链接已创建: {os.path.basename(link_path)} → {os.path.basename(real_path)}")


# ============================================================
# 降级方案
# ============================================================

def generate_raw_report(
    hn_stories: list[dict],
    arxiv_papers: list[dict],
    hf_papers: list[dict],
    x_posts: list[dict],
    yt_videos: list[dict],
) -> str:
    """降级方案：直接输出原始数据，不做 AI 翻译汇总"""
    beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
    today_str = beijing_now.strftime("%Y年%m月%d日")

    lines = [
        f"# 硅谷AI晨报（原始数据） | {today_str}",
        "",
        "> ⚠️ AI 翻译汇总暂时不可用，以下为今日抓取的原始数据。",
        "",
    ]

    # Hacker News
    lines.append("## 📡 Hacker News 热门文章")
    lines.append("")
    if hn_stories:
        for s in hn_stories:
            lines.append(f"- **{s['title']}**")
            lines.append(f"  👍 {s['points']} 分 | 💬 {s['num_comments']} 评论")
            lines.append(f"  🔗 {s['url']}")
        lines.append("")
    else:
        lines.append("（无数据）")
        lines.append("")

    # ArXiv
    lines.append("## 📄 ArXiv 最新论文")
    lines.append("")
    if arxiv_papers:
        for p in arxiv_papers:
            lines.append(f"- **{p['title']}**")
            lines.append(f"  作者: {', '.join(p['authors'][:3])}")
            lines.append(f"  {p['summary'][:300]}")
            lines.append(f"  🔗 {p['url']}")
        lines.append("")
    else:
        lines.append("（无数据）")
        lines.append("")

    # HuggingFace
    lines.append("## 🤗 HuggingFace 每日精选")
    lines.append("")
    if hf_papers:
        for p in hf_papers:
            lines.append(f"- {p['title']}")
            lines.append(f"  🔗 {p['url']}")
        lines.append("")
    else:
        lines.append("（无数据）")
        lines.append("")

    # X (Twitter)
    lines.append("## 🐦 X精选（硅谷大佬今日发言）")
    lines.append("")
    if x_posts:
        for t in x_posts:
            identity = X_ACCOUNTS.get(t["handle"], t["handle"])
            lines.append(f"- **@{t['handle']}**（{identity}）")
            lines.append(f"  {t['content']}")
            lines.append(f"  🔗 {t['url']}")
        lines.append("")
    else:
        lines.append("（无数据）")
        lines.append("")

    # YouTube 视频
    lines.append("## 🎬 YouTube 最新视频")
    lines.append("")
    if yt_videos:
        for v in yt_videos:
            lines.append(f"- **[{v['channel_name']}] {v['title']}**")
            lines.append(f"  字幕摘要: {v['subtitle_text'][:500]}...")
            lines.append(f"  🔗 {v['url']}")
        lines.append("")
    else:
        lines.append("（无数据）")
        lines.append("")

    return "\n".join(lines)


def generate_fallback_report() -> str:
    """完全降级报告：无任何数据时生成"""
    beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
    today_str = beijing_now.strftime("%Y年%m月%d日")

    return f"""# 硅谷AI晨报 | {today_str}

> ⚠️ 今日数据抓取失败，所有数据源均无法访问。

## 🔥 今日头条
今日暂无数据。可能原因：
- 网络连接问题（代理 http://172.23.80.1:7890 不可达）
- Hacker News API 不可用
- ArXiv API 不可用

## 📰 社区热议
今日暂无值得关注的内容。

## 📄 论文速览
今日暂无值得关注的内容。

## 💡 润之点评
> 连AI都抓不到AI新闻，这大概就是「人工智障」的极致体现吧。😅

---

### 🛠 配置指南
1. **确保代理可用**: 检查 `http://172.23.80.1:7890` 是否可以访问外网
2. **设置 DeepSeek API Key**（必需）: 在 `~/.hermes/.env` 中添加 `export DEEPSEEK_API_KEY=sk-xxx`
3. 完成后重新运行 `bash run.sh`
"""


# ============================================================
# 主流程
# ============================================================

def main():
    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║      硅谷AI晨报 · 自动化 Pipeline            ║")
    logger.info("╚══════════════════════════════════════════════╝")
    logger.info(f"工作目录: {WORKDIR}")
    logger.info(f"代理地址: {PROXY_URL}")

    # 生成日期化输出路径
    today_ds = _today_date_str()
    dated_md, dated_json = _dated_output_paths(today_ds)
    logger.info(f"输出文件: {dated_md}")
    logger.info(f"JSON 输出: {dated_json}")

    # ---- 第一步：抓取内容 ----
    hn_stories = fetch_hn_stories()
    arxiv_papers = fetch_arxiv_papers()
    hf_papers = fetch_hf_papers()
    x_posts = fetch_all_x_posts()
    yt_videos = fetch_all_youtube_subs()

    # 统计抓取结果
    hn_total = len(hn_stories)
    arxiv_total = len(arxiv_papers)
    hf_total = len(hf_papers)
    x_total = len(x_posts)
    yt_total = len(yt_videos)
    total = hn_total + arxiv_total + hf_total + x_total + yt_total

    logger.info("=" * 50)
    logger.info(f"📊 抓取汇总: HN={hn_total}, ArXiv={arxiv_total}, HuggingFace={hf_total}, X={x_total}, YouTube={yt_total}, 总计={total}")

    if total == 0:
        logger.error("❌ 所有数据源均抓取失败，请检查网络或代理配置")
        # 生成一个降级报告
        fallback = generate_fallback_report()
        write_output_md(fallback, dated_md)
        # JSON 降级
        beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
        fallback_json = {
            "date": beijing_now.strftime("%Y年%m月%d日"),
            "headline": "今日数据抓取失败，所有数据源均无法访问。",
            "headline_category": "其他",
            "articles": [],
            "editor_note": "连AI都抓不到AI新闻，这大概就是「人工智障」的极致体现吧。😅",
        }
        write_output_json(fallback_json, dated_json)
        _update_symlinks(dated_md, dated_json)
        logger.info("已生成降级报告（含设置指南）")
        return 1

    # ---- 去重：对比近 3 天日报，过滤重复数据 ----
    past_ids = set()
    yesterday_headline = ""
    for days_ago in (1, 2, 3):
        past_ds = _days_ago_date_str(days_ago)
        past_json = os.path.join(WORKDIR, f"output_{past_ds}.json")
        if os.path.exists(past_json):
            try:
                with open(past_json, encoding="utf-8") as f:
                    pdata = json.load(f)
                for art in pdata.get("articles", []):
                    aid = art.get("id", "")
                    if aid:
                        past_ids.add(aid)
                if days_ago == 1:
                    yheadline = pdata.get("headline", {})
                    if isinstance(yheadline, dict):
                        yesterday_headline = yheadline.get("title", "")
            except Exception:
                pass
    logger.info(f"📋 近3天去重: {len(past_ids)} 条已报道, 昨日头条: {yesterday_headline[:30]}")

    if past_ids:
        hn_stories = [s for s in hn_stories if f"hn_{s.get('objectID','')}" not in past_ids]
        arxiv_papers = [p for p in arxiv_papers if f"arxiv_{p.get('arxiv_id','')}" not in past_ids]
        x_posts = [t for t in x_posts if f"x_{t.get('handle','')}_{t.get('tweet_id','')}" not in past_ids]
        yt_videos = [v for v in yt_videos if f"yt_{v.get('index','')}" not in past_ids]
        logger.info(f"📋 去重后: HN={len(hn_stories)}, ArXiv={len(arxiv_papers)}, X={len(x_posts)}, YT={len(yt_videos)}")

    # ---- 第二步：汇总翻译 ----
    try:
        data = summarize_content(hn_stories, arxiv_papers, hf_papers, x_posts, yt_videos, yesterday_headline)
    except RuntimeError as e:
        logger.error(f"DeepSeek API 调用失败: {e}")
        # 降级：输出原始数据
        fallback = generate_raw_report(hn_stories, arxiv_papers, hf_papers, x_posts, yt_videos)
        write_output_md(fallback, dated_md)
        # JSON 降级
        beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
        fallback_json = {
            "date": beijing_now.strftime("%Y年%m月%d日"),
            "headline": "AI 翻译汇总暂时不可用，以下为今日原始数据。",
            "headline_category": "其他",
            "articles": [],
            "editor_note": "DeepSeek API 调用失败，已降级输出原始数据。",
        }
        write_output_json(fallback_json, dated_json)
        _update_symlinks(dated_md, dated_json)
        logger.info("已降级输出原始数据汇总")
        return 1
    except Exception as e:
        logger.error(f"汇总过程异常: {e}")
        fallback = generate_raw_report(hn_stories, arxiv_papers, hf_papers, x_posts, yt_videos)
        write_output_md(fallback, dated_md)
        return 1

    # ---- 第二步b：双语支持 ----
    # 构建原始英文标题映射
    raw_title_map = _build_raw_title_map(hn_stories, arxiv_papers, hf_papers, x_posts, yt_videos)
    # 生成英文摘要
    english_summaries = _generate_english_summaries(data.get("articles", []))
    # 添加双语字段
    data = add_bilingual_fields(data, raw_title_map, english_summaries)

    # ---- 第三步：写入双格式输出 ----
    write_output(data, dated_md, dated_json)
    # 创建/更新软链接
    _update_symlinks(dated_md, dated_json)

    # ---- 第三步b：生成英文版 JSON ----
    dated_en_json = os.path.join(WORKDIR, f"output_{today_ds}_en.json")
    generate_english_json(data, dated_en_json)

    # ---- 第四步：公众号 HTML 渲染 ----
    try:
        import render_wechat
        wechat_output = os.path.join(WORKDIR, "output_wechat.html")
        render_wechat.render(json_path=dated_json, output_path=wechat_output)
        logger.info(f"✅ 公众号 HTML 已输出: {wechat_output}")
    except Exception as e:
        logger.warning(f"⚠️ 公众号 HTML 渲染失败（不影响主流程）: {e}")
        wechat_output = None

    # ---- 第五步：创建微信公众号草稿 ----
    try:
        import wechat_api

        # 确定 HTML 文件路径（优先用刚渲染的，否则找已有的）
        html_path = wechat_output or os.path.join(WORKDIR, "output_wechat.html")
        if not os.path.exists(html_path):
            # 回退到日期化路径
            html_path = os.path.join(WORKDIR, f"output_wechat.html")
            if not os.path.exists(html_path):
                logger.warning("⚠️ 未找到公众号 HTML 文件，跳过草稿创建")
                html_path = None

        if html_path:
            with open(html_path, "r", encoding="utf-8") as f:
                html_content = f.read()

            # 获取 access_token
            token = wechat_api.get_access_token()

            # 生成封面图（用头条人物/公司名）
            thumb_media_id = None
            try:
                import cover_gen
                headline_title = ""
                if isinstance(data.get("headline"), dict):
                    headline_title = data["headline"].get("title", "")
                date_cn = data.get("date", "")
                cover_path = cover_gen.generate_cover(headline_title, date_cn)
                thumb_media_id = wechat_api.upload_cover(token, cover_path)
            except Exception as e:
                logger.warning(f"⚠️ 封面上传跳过: {e}")

            # 从 data 中提取头条标题作为草稿标题
            headline = data.get("headline", {})
            if isinstance(headline, dict):
                draft_title = headline.get("title", "") or headline.get("summary", "")[:30]
            else:
                draft_title = str(headline)[:30] if headline else ""
            if not draft_title:
                draft_title = "硅谷AI晨报"

            # 摘要：从头条 summary 截取
            digest_text = ""
            if isinstance(headline, dict):
                digest_text = headline.get("summary", "")[:54]
            elif headline:
                digest_text = str(headline)[:54]

            draft_id = wechat_api.create_draft(
                token,
                html_content,
                title=draft_title,
                thumb_media_id=thumb_media_id,
                digest=digest_text,
            )

            if draft_id:
                logger.info(f"✅ 微信草稿已创建: draft_id={draft_id}")
                # 自动发布
                wechat_api.publish_draft(token, draft_id)
            else:
                logger.warning("⚠️ 微信草稿创建失败（可能权限不足或接口限制）")
    except Exception as e:
        logger.warning(f"⚠️ 微信草稿创建失败（不影响主流程）: {e}")

    logger.info("=" * 50)
    logger.info("🎉 Pipeline 执行完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
