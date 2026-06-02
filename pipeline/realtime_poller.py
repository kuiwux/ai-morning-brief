#!/usr/bin/env python3
"""
5 分钟实时轮询守护进程 — 18 个数据源抓取 + DeepSeek 翻译去重 + SQLite 存储

架构：
  主循环（每 5 分钟）：
    T+0s    → 并行抓取 18 个数据源（ThreadPoolExecutor, max_workers=10）
    T+30s   → DeepSeek 翻译摘要 + 分类 + 去重（3天窗口）
    T+60s   → 写入 SQLite + 检测新增内容
    T+90s   → 有新内容 → push 到 notification_queue
    T+300s  → 下一轮
"""

import os
import sys
import json
import re
import html
import time
import signal
import logging
import hashlib
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from typing import Optional
from difflib import SequenceMatcher

import requests
from openai import OpenAI

# ── 路径 ───────────────────────────────────────────────────────────────────
WORKDIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKDIR)

from database import (
    init_db, insert_article, get_recent_article_urls,
    get_recent_article_ids, get_recent_article_titles,
    enqueue_notification,
)

# ── 全局代理配置 ────────────────────────────────────────────────────────────
PROXY_URL = os.environ.get("https_proxy", "http://172.23.80.1:7890")

session = requests.Session()
session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; AiMorningPoller/3.0)"})

# ── 配置 ────────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 300))  # 5 分钟
DEDUP_WINDOW_DAYS = 3
SOURCE_TIMEOUT = 30           # 每个数据源超时（秒）
ROUND_TIMEOUT = 180           # 整轮超时（秒）
MAX_CONCURRENT_FETCH = 10     # 最大并发抓取数
TITLE_SIMILARITY_THRESHOLD = 0.8  # 标题相似度去重阈值

DEEPSEEK_MODEL = "deepseek-chat"
HTTP_TIMEOUT = 30

# ── 日志 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("poller")

# ── 优雅退出 ────────────────────────────────────────────────────────────────
_shutdown_requested = False
_current_round_done = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    sig_name = signal.Signals(signum).name
    logger.info(f"收到 {sig_name} 信号，将在当前轮次完成后退出...")
    _shutdown_requested = True


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

# ── 数据源定义 ──────────────────────────────────────────────────────────────

# 硅谷 10 源
SILICON_VALLEY_SOURCES = [
    "hn", "arxiv", "huggingface", "x_twitter", "youtube",
    "techcrunch", "venturebeat", "theverge", "openai_blog", "anthropic_blog",
    "mit_tech_review", "wired",
]

# 国内 8 源
CHINA_SOURCES = [
    "cn_jiqizhixin", "cn_liangziwei", "cn_36kr",
    "cn_baidu_ai", "cn_aliyun", "cn_deepseek", "cn_zhipu", "cn_jike",
]

ALL_SOURCES = SILICON_VALLEY_SOURCES + CHINA_SOURCES

# X (Twitter) 账号 — 扩展至 20+
X_ACCOUNTS = {
    "kaboroehart":  "Andrej Karpathy",
    "sama":         "Sam Altman",
    "jimfan":       "Jim Fan",
    "ylecun":       "Yann LeCun",
    "_akhaliq":     "AK（AI论文速递）",
    "nrehiew_":     "Nino（AI资讯）",
    "DrJimFan":     "Jim Fan",
    "gdb":          "Greg Brockman",
    "miramurati":   "Mira Murati",
    "ilyasut":      "Ilya Sutskever",
    "satyanadella": "Satya Nadella",
    "demishassabis":"Demis Hassabis",
    "darioamodei":  "Dario Amodei",
    "drfeifei":     "Fei-Fei Li",
    "AndrewYNg":    "Andrew Ng",
    "emostaque":    "Emad Mostaque",
    "aidan_mclau":  "Aidan McLau",
    "bindureddy":   "Niki Parmar",
    "nvidia":       "NVIDIA",
    "OpenAI":       "OpenAI",
    "GoogleAI":     "Google AI",
}

# YouTube 频道
YOUTUBE_CHANNELS = {
    "OpenAI":            "@OpenAI",
    "Google DeepMind":   "@GoogleDeepMind",
    "Anthropic":         "@AnthropicAI",
    "NVIDIA":            "@NVIDIA",
    "Two Minute Papers": "@TwoMinutePapers",
    "Yannic Kilcher":    "@YannicKilcher",
    "AI Explained":      "@aiexplained-official",
}

# RSS 源定义
RSS_FEEDS = {
    "techcrunch":    "https://techcrunch.com/feed/",
    "venturebeat":   "https://venturebeat.com/feed/",
    "theverge":      "https://www.theverge.com/rss/index.xml",
    "openai_blog":   "https://openai.com/blog/rss.xml",
    "anthropic_blog": "https://www.anthropic.com/blog/rss.xml",
    "mit_tech_review": "https://www.technologyreview.com/feed/",
    "wired":         "https://www.wired.com/feed/category/ai/latest/rss",
    "cn_jiqizhixin": "https://www.jiqizhixin.com/rss",
    "cn_liangziwei": "https://www.qbitai.com/feed",
    "cn_36kr":       "https://36kr.com/feed",
}

YT_SUBS_DIR = "/tmp/yt_subs"

# ── DeepSeek 客户端 ─────────────────────────────────────────────────────────

def get_deepseek_client() -> OpenAI:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        env_file = os.path.join(WORKDIR, ".env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY="):
                        api_key = line.split("=", 1)[1]
                        break
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置")
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


# ── 工具函数 ────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text.strip()


def make_id(source_type: str, unique: str) -> str:
    """生成唯一文章 ID，如 hn_123456, arxiv_2405.xxx"""
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(unique))
    return f"{source_type}_{safe}"


def title_similarity(t1: str, t2: str) -> float:
    """计算两个标题的相似度"""
    if not t1 or not t2:
        return 0.0
    return SequenceMatcher(None, t1.lower(), t2.lower()).ratio()


def _extract_tweet_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else ""


# ══════════════════════════════════════════════════════════════════════════════
# 数据源抓取函数
# ══════════════════════════════════════════════════════════════════════════════

# ── 1. Hacker News (Algolia API) ───────────────────────────────────────────
def _hn_search(query: str, max_hits: int = 20) -> list[dict]:
    try:
        resp = session.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": query, "tags": "story", "hitsPerPage": max_hits, "numericFilters": "points>10"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [
            {
                "title": h.get("title", ""),
                "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}",
                "points": h.get("points", 0),
                "num_comments": h.get("num_comments", 0),
                "created_at": h.get("created_at", ""),
                "objectID": h.get("objectID", ""),
                "author": h.get("author", ""),
            }
            for h in data.get("hits", [])
        ]
    except Exception:
        return []


def fetch_hn() -> list[dict]:
    query_groups = [
        "AI OR LLM OR OpenAI OR GPT OR deepseek",
        "Anthropic OR Claude OR Gemini OR Llama OR Mistral",
        "diffusion OR transformer OR AGI OR RLHF OR RAG",
        '"Sam Altman" OR "Andrej Karpathy" OR "Jim Fan" OR "Yann LeCun" OR "Jensen Huang"',
        '"Demis Hassabis" OR "Dario Amodei" OR "Ilya Sutskever" OR "Elon Musk AI" OR "Satya Nadella AI"',
        '"Nvidia CEO" OR "OpenAI CEO" OR "DeepMind" OR "Anthropic CEO" OR "SSI"',
    ]
    seen = set()
    items = []
    for q in query_groups:
        for hit in _hn_search(q, max_hits=10):
            oid = hit["objectID"]
            if oid not in seen:
                seen.add(oid)
                items.append({
                    "id": make_id("hn", oid),
                    "title": hit["title"],
                    "source_url": hit["url"],
                    "source_type": "hn",
                    "source_region": "silicon_valley",
                    "published_at": hit["created_at"],
                    "raw": hit,
                })
    items.sort(key=lambda x: x["raw"]["points"], reverse=True)
    return items[:10]


# ── 2. ArXiv API ────────────────────────────────────────────────────────────
def fetch_arxiv() -> list[dict]:
    url = (
        "http://export.arxiv.org/api/query"
        "?search_query=cat:cs.AI+OR+cat:cs.CL+OR+cat:cs.LG"
        "&sortBy=submittedDate&sortOrder=descending&max_results=10"
    )
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.text)
        papers = []
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            id_el = entry.find("atom:id", ns)
            published_el = entry.find("atom:published", ns)
            author_els = entry.findall("atom:author/atom:name", ns)
            authors = [a.text.strip() for a in author_els if a.text]

            arxiv_id = id_el.text.strip() if id_el is not None and id_el.text else ""
            short_id = arxiv_id.split("/abs/")[-1] if "/abs/" in arxiv_id else arxiv_id
            title = strip_html(title_el.text) if title_el is not None and title_el.text else ""
            summary = strip_html(summary_el.text) if summary_el is not None and summary_el.text else ""
            if len(summary) > 800:
                summary = summary[:800] + "..."

            papers.append({
                "id": make_id("arxiv", short_id),
                "title": title,
                "summary_raw": summary,
                "source_url": f"https://arxiv.org/abs/{short_id}",
                "source_type": "arxiv",
                "source_region": "silicon_valley",
                "published_at": published_el.text if published_el is not None else "",
                "authors": authors,
            })
        return papers
    except Exception:
        return []


# ── 3. HuggingFace Papers ───────────────────────────────────────────────────
def fetch_huggingface() -> list[dict]:
    url = "https://huggingface.co/papers"
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return []
        html_content = resp.text
        # 尝试从 HTML 提取标题和链接
        paper_links = re.findall(
            r'<a[^>]*href="(/papers/[^"]+)"[^>]*>(.*?)</a>',
            html_content, re.DOTALL,
        )
        seen = set()
        papers = []
        for link, raw_title in paper_links[:10]:
            title = strip_html(raw_title).strip()
            if title and len(title) > 10 and link not in seen:
                seen.add(link)
                short_id = link.split("/")[-1] if "/" in link else link
                papers.append({
                    "id": make_id("hf", short_id),
                    "title": title,
                    "source_url": f"https://huggingface.co{link}",
                    "source_type": "hf",
                    "source_region": "silicon_valley",
                    "published_at": "",
                })
        return papers
    except Exception:
        return []


# ── 4. X/Twitter (Nitter RSS) ───────────────────────────────────────────────
def _fetch_x_single(handle: str) -> list[dict]:
    url = f"https://nitter.net/{handle}/rss"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.text)
        tweets = []
        for item in root.findall(".//item")[:3]:  # 每人最多 3 条
            title_el = item.find("title")
            link_el = item.find("link")
            content = title_el.text if title_el is not None and title_el.text else ""
            content = re.sub(r"^R to @\w+\s*", "", content).strip()
            link_text = link_el.text if link_el is not None and link_el.text else ""
            tweet_id = _extract_tweet_id(link_text)
            if content:
                tweets.append({
                    "id": make_id("x", f"{handle}_{tweet_id}" if tweet_id else f"{handle}_{hash(content)}"),
                    "title": content[:150],
                    "source_url": link_text,
                    "source_type": "x",
                    "source_region": "silicon_valley",
                    "published_at": "",
                    "handle": handle,
                    "identity": X_ACCOUNTS.get(handle, handle),
                })
        return tweets
    except Exception:
        return []


def fetch_x_twitter() -> list[dict]:
    all_tweets = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_fetch_x_single, h): h for h in X_ACCOUNTS}
        for f in as_completed(futures, timeout=30):
            try:
                all_tweets.extend(f.result(timeout=10))
            except Exception:
                pass
    return all_tweets


# ── 5. YouTube ──────────────────────────────────────────────────────────────
def fetch_youtube() -> list[dict]:
    import subprocess
    os.makedirs(YT_SUBS_DIR, exist_ok=True)
    videos = []
    for channel_name, handle in YOUTUBE_CHANNELS.items():
        channel_url = f"https://www.youtube.com/@{handle.lstrip('@')}"
        try:
            id_result = subprocess.run(
                ["yt-dlp", "--flat-playlist", "--playlist-end", "1", "--print", "id",
                 "--proxy", PROXY_URL, "--js-runtimes", "deno", channel_url],
                capture_output=True, text=True, timeout=30,
            )
            if id_result.returncode != 0:
                continue
            video_id = id_result.stdout.strip()
            if not video_id:
                continue

            video_url = f"https://www.youtube.com/watch?v={video_id}"
            title_result = subprocess.run(
                ["yt-dlp", "--print", "title", "--proxy", PROXY_URL,
                 "--js-runtimes", "deno", video_url],
                capture_output=True, text=True, timeout=20,
            )
            title = title_result.stdout.strip() if title_result.returncode == 0 else ""

            videos.append({
                "id": make_id("yt", video_id),
                "title": title,
                "source_url": video_url,
                "source_type": "yt",
                "source_region": "silicon_valley",
                "published_at": "",
                "channel_name": channel_name,
            })
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        except Exception:
            continue
    return videos


# ── 6-10. RSS 源（通用）─────────────────────────────────────────────────────
def _fetch_rss(source_key: str, url: str, ai_only: bool = True) -> list[dict]:
    """通用 RSS 抓取器，返回标准化文章列表"""
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.text)

        # 兼容 RSS 2.0 / Atom
        items = []
        # 先尝试 RSS 2.0 <channel><item>
        channel = root.find(".//channel")
        if channel is not None:
            items = channel.findall("item")
        if not items:
            # 尝试 Atom <entry>
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall("atom:entry", ns) or root.findall(".//{http://www.w3.org/2005/Atom}entry")

        articles = []
        for item in items[:10]:
            # RSS 2.0
            title_el = item.find("title") or item.find(".//{http://www.w3.org/2005/Atom}title")
            link_el = item.find("link") or item.find(".//{http://www.w3.org/2005/Atom}link")
            desc_el = item.find("description") or item.find(".//{http://www.w3.org/2005/Atom}summary")
            pub_el = item.find("pubDate") or item.find(".//{http://www.w3.org/2005/Atom}published") or item.find(".//{http://www.w3.org/2005/Atom}updated")

            title = title_el.text if title_el is not None and title_el.text else ""
            title = strip_html(title)
            link = link_el.text if link_el is not None else ""
            if link_el is not None and link_el.get("href"):
                link = link_el.get("href")
            published = pub_el.text if pub_el is not None and pub_el.text else ""

            if not title:
                continue

            # AI 内容过滤（对科技媒体 RSS）
            if ai_only:
                ai_keywords = ["AI", "LLM", "GPT", "artificial intelligence", "machine learning",
                               "deep learning", "neural network", "transformer", "Claude",
                               "Gemini", "OpenAI", "Anthropic", "DeepMind", "NVIDIA",
                               "language model", "diffusion", "AGI", "RLHF", "RAG"]
                title_lower = title.lower()
                if not any(kw.lower() in title_lower for kw in ai_keywords):
                    continue

            articles.append({
                "id": make_id(source_key, hashlib.md5(link.encode()).hexdigest()[:12]),
                "title": title,
                "source_url": link,
                "source_type": source_key,
                "source_region": "silicon_valley",
                "published_at": published,
            })
        return articles
    except Exception:
        return []


def fetch_techcrunch() -> list[dict]:
    return _fetch_rss("tc", RSS_FEEDS["techcrunch"])

def fetch_venturebeat() -> list[dict]:
    return _fetch_rss("vb", RSS_FEEDS["venturebeat"])

def fetch_theverge() -> list[dict]:
    return _fetch_rss("verge", RSS_FEEDS["theverge"])

def fetch_openai_blog() -> list[dict]:
    return _fetch_rss("openai_blog", RSS_FEEDS["openai_blog"], ai_only=False)

def fetch_anthropic_blog() -> list[dict]:
    return _fetch_rss("anthropic_blog", RSS_FEEDS["anthropic_blog"], ai_only=False)

def fetch_mit_tech_review() -> list[dict]:
    return _fetch_rss("mit_tech_review", RSS_FEEDS["mit_tech_review"])

def fetch_wired() -> list[dict]:
    return _fetch_rss("wired", RSS_FEEDS["wired"])


# ── 11-13. 国内 RSS 源 ──────────────────────────────────────────────────────
def fetch_cn_jiqizhixin() -> list[dict]:
    items = _fetch_rss("cn_jiqizhixin", RSS_FEEDS["cn_jiqizhixin"], ai_only=False)
    for item in items:
        item["source_region"] = "china"
    return items

def fetch_cn_liangziwei() -> list[dict]:
    items = _fetch_rss("cn_liangziwei", RSS_FEEDS["cn_liangziwei"], ai_only=False)
    for item in items:
        item["source_region"] = "china"
    return items

def fetch_cn_36kr() -> list[dict]:
    items = _fetch_rss("cn_36kr", RSS_FEEDS["cn_36kr"], ai_only=False)
    for item in items:
        item["source_region"] = "china"
    return items


# ── 14-18. 国内企业官网抓取（网页解析）─────────────────────────────────────
def _fetch_html_titles(source_key: str, url: str, selectors: list[str]) -> list[dict]:
    """通用 HTML 解析：提取标题和链接"""
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return []
        html_content = resp.text

        articles = []
        seen_urls = set()

        for selector in selectors:
            # 简化正则匹配常见模式
            patterns = [
                r'<a[^>]*href="([^"]*)"[^>]*>\s*(.*?)\s*</a>',
            ]
            for pattern in patterns:
                for match in re.finditer(pattern, html_content, re.DOTALL):
                    href = match.group(1)
                    title = strip_html(match.group(2))
                    if not title or len(title) < 5:
                        continue
                    # 过滤非文章链接
                    if any(skip in href.lower() for skip in ['login', 'signup', 'javascript:', '#']):
                        continue
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)

                    # 补全相对 URL
                    if href.startswith("/"):
                        base = "/".join(url.split("/")[:3])
                        href = base + href

                    articles.append({
                        "id": make_id(source_key, hashlib.md5(title.encode()).hexdigest()[:12]),
                        "title": title.strip()[:200],
                        "source_url": href,
                        "source_type": source_key,
                        "source_region": "china",
                        "published_at": "",
                    })

                if len(articles) >= 5:
                    break
            if len(articles) >= 5:
                break

        return articles[:5]
    except Exception:
        return []


def fetch_cn_baidu_ai() -> list[dict]:
    # 百度AI 动态
    return _fetch_html_titles("cn_baidu_ai", "https://cloud.baidu.com/product/wenxinworkshop.html",
                              ["a[href*='news']", "a[href*='blog']"])

def fetch_cn_aliyun() -> list[dict]:
    # 阿里云 AI 动态
    return _fetch_html_titles("cn_aliyun", "https://www.aliyun.com/product/tongyi",
                              ["a[href*='news']", "a[href*='announcement']"])

def fetch_cn_deepseek() -> list[dict]:
    # 深度求索 官方公告
    try:
        resp = session.get("https://api-docs.deepseek.com/", timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return []
        html_content = resp.text
        # 提取标题
        titles = re.findall(r'<h[1-4][^>]*>(.*?)</h[1-4]>', html_content, re.DOTALL)
        links = re.findall(r'<a[^>]*href="(https?://[^"]*deepseek[^"]*)"[^>]*>(.*?)</a>', html_content, re.DOTALL)
        articles = []
        for i, t in enumerate(titles[:5]):
            title = strip_html(t)
            if title and len(title) > 5:
                articles.append({
                    "id": make_id("cn_deepseek", hashlib.md5(title.encode()).hexdigest()[:12]),
                    "title": title[:200],
                    "source_url": "https://api-docs.deepseek.com/",
                    "source_type": "cn_deepseek",
                    "source_region": "china",
                    "published_at": "",
                })
        return articles
    except Exception:
        return []

def fetch_cn_zhipu() -> list[dict]:
    # 智谱AI 动态
    return _fetch_html_titles("cn_zhipu", "https://open.bigmodel.cn/",
                              ["a[href*='blog']", "a[href*='news']", "a[href*='announcement']"])

def fetch_cn_jike() -> list[dict]:
    # 即刻AI圈 — 使用即刻 API 或网页抓取
    try:
        resp = session.get("https://web.okjike.com/topic/553870c6e4b0b51ee175a39e/official", timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return []
        html_content = resp.text
        # 从页面内容提取帖子标题
        titles = re.findall(r'"content"\s*:\s*"([^"]+)"', html_content)
        articles = []
        for title in titles[:5]:
            title = title.replace("\\n", " ").strip()
            if title and len(title) > 5:
                articles.append({
                    "id": make_id("cn_jike", hashlib.md5(title.encode()).hexdigest()[:12]),
                    "title": title[:200],
                    "source_url": "https://web.okjike.com/topic/553870c6e4b0b51ee175a39e/official",
                    "source_type": "cn_jike",
                    "source_region": "china",
                    "published_at": "",
                })
        return articles
    except Exception:
        return []


# ── 所有抓取函数映射 ────────────────────────────────────────────────────────
FETCH_FUNCTIONS = {
    "hn": fetch_hn,
    "arxiv": fetch_arxiv,
    "huggingface": fetch_huggingface,
    "x_twitter": fetch_x_twitter,
    "youtube": fetch_youtube,
    "techcrunch": fetch_techcrunch,
    "venturebeat": fetch_venturebeat,
    "theverge": fetch_theverge,
    "openai_blog": fetch_openai_blog,
    "anthropic_blog": fetch_anthropic_blog,
    "mit_tech_review": fetch_mit_tech_review,
    "wired": fetch_wired,
    "cn_jiqizhixin": fetch_cn_jiqizhixin,
    "cn_liangziwei": fetch_cn_liangziwei,
    "cn_36kr": fetch_cn_36kr,
    "cn_baidu_ai": fetch_cn_baidu_ai,
    "cn_aliyun": fetch_cn_aliyun,
    "cn_deepseek": fetch_cn_deepseek,
    "cn_zhipu": fetch_cn_zhipu,
    "cn_jike": fetch_cn_jike,
}


# ══════════════════════════════════════════════════════════════════════════════
# DeepSeek 翻译 + 分类
# ══════════════════════════════════════════════════════════════════════════════

TRANSLATION_PROMPT_TEMPLATE = """你是一位专业的AI行业分析师。请对以下AI资讯内容进行中文摘要和分类。

## 版权保护规则（必须严格遵守）
1. 禁止直接复制原文：绝对不能逐句翻译或复制原始内容。必须用自己的语言重新组织表达。
2. 必须深度改写：调整句式结构、重新组织信息顺序、用自己的话总结核心要点。
3. 引用不等于复制：用「据XX报道」「XX指出」等转述方式，不得直接引述原文。

## 内容信息
- 来源: {source_type}（{source_region}）
- 标题: {title}
{extra_info}

## 输出要求

请你直接输出一个纯 JSON 对象（不要 markdown 代码块），格式如下：

{{
  "title_cn": "中文标题（专有名词保留英文，如 LLM、ChatGPT、API 等）",
  "summary_cn": "2-3句中英文摘要，约80-120字，说明核心价值和为什么值得关注",
  "category": "公司动态/技术突破/产品发布/观点评论/学术前沿 之一",
  "tags": ["标签1", "标签2"],
  "credibility": "high/medium/low"
}}

## 分类规则
- 「公司动态」：公司战略、融资、人事变动、政策监管
- 「技术突破」：新技术、模型发布、研究突破、性能飞跃
- 「产品发布」：新产品、新功能、开源项目、工具发布
- 「观点评论」：名人发言、行业观点、趋势分析、预测展望
- 「学术前沿」：论文、研究方法、理论突破

## Tags 要求
- 提取涉及的核心人物和组织，如 ["Sam Altman", "OpenAI"]
- 至少 1 个、最多 5 个
- 只填真实出现的实体，不要猜测

## 可信度评分
- high: 官方公告、学术论文、一手采访
- medium: 知名科技媒体报道
- low: 社交媒体、非一手来源

直接以 {{ 开头，}} 结尾，不要任何额外文字。"""


def translate_article(article: dict) -> Optional[dict]:
    """对单篇文章调用 DeepSeek 进行翻译和分类"""
    client = get_deepseek_client()

    extra_info = ""
    if article.get("authors"):
        extra_info += f"- 作者: {', '.join(article['authors'][:3])}\n"
    if article.get("summary_raw"):
        extra_info += f"- 原始摘要: {article['summary_raw'][:500]}\n"
    if article.get("handle"):
        extra_info += f"- 发推人: @{article['handle']} ({article.get('identity','')})\n"

    source_region = article.get("source_region", "silicon_valley")
    region_label = "硅谷" if source_region == "silicon_valley" else "中国"

    prompt = TRANSLATION_PROMPT_TEMPLATE.format(
        source_type=article.get("source_type", ""),
        source_region=region_label,
        title=article.get("title", ""),
        extra_info=extra_info,
    )

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "你是一位专业的AI行业分析师。永远只输出合法的 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=1024,
        )
        content = response.choices[0].message.content.strip()

        # 解析 JSON
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)

        # 用 brace 计数器提取
        start = content.find("{")
        if start == -1:
            return None
        depth = 0
        end = -1
        for i in range(start, len(content)):
            ch = content[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        if end > 0:
            result = json.loads(content[start:end])
        else:
            result = json.loads(content)

        # 合并回 article
        article["title_cn"] = result.get("title_cn", "")
        article["summary_cn"] = result.get("summary_cn", "")
        article["category"] = result.get("category", "技术突破")
        article["tags"] = result.get("tags", [])
        article["credibility"] = result.get("credibility", "medium")
        article["summary"] = article.get("summary_raw", "")[:200]
        article["ai_comment"] = ""
        article["eli5"] = ""
        article["source_count"] = 1
        article["is_pushed"] = 0

        return article

    except Exception as e:
        logger.warning(f"DeepSeek 翻译失败 [{article.get('id','?')}]: {e}")
        # 降级：用原始数据填充
        article["title_cn"] = article.get("title", "")
        article["summary_cn"] = article.get("summary_raw", "")[:200]
        article["category"] = "技术突破"
        article["tags"] = []
        article["credibility"] = "low"
        article["summary"] = article.get("summary_raw", "")[:200]
        article["ai_comment"] = ""
        article["eli5"] = ""
        article["source_count"] = 1
        article["is_pushed"] = 0
        return article


def translate_batch(articles: list[dict]) -> list[dict]:
    """并发翻译一批文章"""
    translated = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(translate_article, a): a for a in articles}
        for f in as_completed(futures, timeout=120):
            try:
                result = f.result(timeout=30)
                if result:
                    translated.append(result)
            except Exception as e:
                logger.warning(f"翻译线程异常: {e}")
    return translated


# ══════════════════════════════════════════════════════════════════════════════
# 去重
# ══════════════════════════════════════════════════════════════════════════════

def deduplicate(articles: list[dict]) -> list[dict]:
    """基于 URL、ID 和标题相似度去重（3 天窗口）"""
    if not articles:
        return []

    # 从数据库加载已有数据
    existing_ids = get_recent_article_ids(DEDUP_WINDOW_DAYS)
    existing_urls = get_recent_article_urls(DEDUP_WINDOW_DAYS)
    existing_titles = get_recent_article_titles(DEDUP_WINDOW_DAYS)

    logger.info(f"去重参考: {len(existing_ids)} IDs, {len(existing_urls)} URLs, {len(existing_titles)} titles")

    # 当前批次内部去重（按 URL 优先）
    seen_urls = set()
    seen_ids = set()
    deduped = []

    for article in articles:
        aid = article.get("id", "")
        url = article.get("source_url", "")

        # ID 去重
        if aid in existing_ids or aid in seen_ids:
            continue

        # URL 去重
        if url and (url in existing_urls or url in seen_urls):
            continue

        # 标题相似度去重
        title = article.get("title", "")
        is_dup = False
        if title:
            for existing_id, existing_title in existing_titles:
                if title_similarity(title, existing_title) > TITLE_SIMILARITY_THRESHOLD:
                    is_dup = True
                    break

            # 当前批次内部标题去重
            if not is_dup:
                for prev in deduped:
                    if title_similarity(title, prev.get("title", "")) > TITLE_SIMILARITY_THRESHOLD:
                        is_dup = True
                        break

        if is_dup:
            continue

        seen_urls.add(url)
        seen_ids.add(aid)
        deduped.append(article)

    logger.info(f"去重: {len(articles)} → {len(deduped)} 条新增")
    return deduped


# ══════════════════════════════════════════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════════════════════════════════════════

def fetch_all_sources() -> list[dict]:
    """并行抓取所有数据源"""
    all_articles = []
    fetch_errors = []

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCH) as ex:
        futures = {}
        for source_name, fetch_fn in FETCH_FUNCTIONS.items():
            futures[ex.submit(fetch_fn)] = source_name

        for f in as_completed(futures, timeout=ROUND_TIMEOUT):
            source_name = futures[f]
            try:
                articles = f.result(timeout=SOURCE_TIMEOUT)
                all_articles.extend(articles)
                if articles:
                    logger.info(f"  ✓ {source_name}: {len(articles)} 条")
                else:
                    logger.info(f"  - {source_name}: 0 条")
            except FutureTimeoutError:
                logger.warning(f"  ⚠ {source_name}: 超时（{SOURCE_TIMEOUT}s）")
                fetch_errors.append(f"{source_name}: timeout")
            except Exception as e:
                logger.warning(f"  ⚠ {source_name}: 失败 - {e}")
                fetch_errors.append(f"{source_name}: {e}")

    return all_articles


def run_poll_cycle() -> dict:
    """执行一轮完整的轮询周期"""
    cycle_start = time.time()
    logger.info("=" * 60)
    logger.info(f"🔄 开始第 {_global_cycle_count + 1} 轮轮询...")
    logger.info("=" * 60)

    stats = {
        "fetched": 0,
        "new": 0,
        "translated": 0,
        "errors": [],
        "duration_seconds": 0,
    }

    # ── 阶段 1: 抓取（T+0s ~ T+30s）─────────────
    phase1_start = time.time()
    logger.info("📡 [阶段 1/4] 并行抓取 18 个数据源...")
    raw_articles = fetch_all_sources()
    stats["fetched"] = len(raw_articles)
    logger.info(f"📊 抓取完成: {len(raw_articles)} 条，耗时 {time.time() - phase1_start:.1f}s")

    if not raw_articles:
        stats["duration_seconds"] = time.time() - cycle_start
        return stats

    # ── 阶段 2: 翻译（T+30s ~ T+60s，不等所有源完成，每个源抓取后立即翻译）─────────────
    phase2_start = time.time()
    logger.info(f"🤖 [阶段 2/4] DeepSeek 翻译 {len(raw_articles)} 条...")
    translated = translate_batch(raw_articles)
    stats["translated"] = len(translated)
    logger.info(f"🤖 翻译完成: {len(translated)} 条，耗时 {time.time() - phase2_start:.1f}s")

    if not translated:
        stats["duration_seconds"] = time.time() - cycle_start
        return stats

    # ── 阶段 3: 去重 + 写入（T+60s ~ T+90s）─────────────
    phase3_start = time.time()
    logger.info(f"🔍 [阶段 3/4] 去重（{DEDUP_WINDOW_DAYS}天窗口）...")
    deduped = deduplicate(translated)
    stats["new"] = len(deduped)
    logger.info(f"🔍 去重完成: {len(deduped)} 条新增")

    if deduped:
        logger.info("💾 写入 SQLite...")
        write_count = 0
        for article in deduped:
            # 过滤掉 translation 内部字段
            db_article = {
                "id": article.get("id", ""),
                "title": article.get("title", ""),
                "title_cn": article.get("title_cn", ""),
                "summary": article.get("summary", ""),
                "summary_cn": article.get("summary_cn", ""),
                "ai_comment": article.get("ai_comment", ""),
                "eli5": article.get("eli5", ""),
                "category": article.get("category", ""),
                "tags": article.get("tags", []),
                "source_type": article.get("source_type", ""),
                "source_region": article.get("source_region", ""),
                "source_url": article.get("source_url", ""),
                "credibility": article.get("credibility", "medium"),
                "source_count": article.get("source_count", 1),
                "published_at": article.get("published_at", ""),
                "is_pushed": article.get("is_pushed", 0),
            }
            if insert_article(db_article):
                write_count += 1
        logger.info(f"💾 写入完成: {write_count}/{len(deduped)} 条")
    logger.info(f"📊 阶段 3 耗时: {time.time() - phase3_start:.1f}s")

    # ── 阶段 4: 推送（T+90s ~ T+120s）─────────────
    phase4_start = time.time()
    if deduped:
        logger.info(f"📲 [阶段 4/4] 推送 {len(deduped)} 条到 notification_queue...")
        for article in deduped:
            credibility = article.get("credibility", "medium")
            category = article.get("category", "")
            title = article.get("title_cn") or article.get("title", "")

            # 判断是否重要事件（breaking）
            is_breaking = False
            if credibility == "high":
                # 融资/发布类
                funding_keywords = ["融资", "funding", "raise", "billion", "million",
                                    "IPO", "收购", "acquisition", "acquires"]
                release_keywords = ["发布", "release", "launch", "announce", "开源",
                                    "open source", "GPT", "Gemini", "Claude", "Llama"]
                title_lower = title.lower()
                if any(kw.lower() in title_lower for kw in funding_keywords + release_keywords):
                    is_breaking = True

            push_type = "breaking" if is_breaking else "realtime"
            priority = "high" if is_breaking else "normal"

            enqueue_notification(
                article_id=article.get("id", ""),
                push_type=push_type,
                priority=priority,
                payload={
                    "title": title,
                    "category": category,
                    "source_type": article.get("source_type", ""),
                    "credibility": credibility,
                },
            )

        logger.info(f"📲 推送队列写入完成: {len(deduped)} 条 (breaking={sum(1 for a in deduped if a.get('credibility') == 'high')}), 耗时 {time.time() - phase4_start:.1f}s")

        # ── 阶段 4b: 消费推送队列 ──
        phase4b_start = time.time()
        try:
            from push_service import get_push_service
            svc = get_push_service()
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            sent_count = loop.run_until_complete(svc.process_queue(limit=50))
            loop.close()
            logger.info(f"📲 推送队列消费完成: {sent_count} 条通知已发送，耗时 {time.time() - phase4b_start:.1f}s")
        except ImportError:
            logger.info("📲 push_service 未加载，跳过推送消费（仅写入队列）")
        except Exception as e:
            logger.warning(f"📲 推送消费异常: {e}")

    else:
        logger.info("📲 [阶段 4/4] 无新增内容，跳过推送")

    # ── 保存状态 ──
    stats["duration_seconds"] = time.time() - cycle_start
    save_state(stats)
    return stats


_global_cycle_count = 0


def save_state(stats: dict) -> None:
    """保存当前状态到 poller_state.json"""
    state = {
        "last_cycle": datetime.now(timezone.utc).isoformat(),
        "cycle_count": _global_cycle_count,
        "last_stats": stats,
    }
    state_path = os.path.join(WORKDIR, "poller_state.json")
    try:
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"保存状态失败: {e}")


def main():
    global _global_cycle_count

    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║    AI 晨报 · 实时轮询系统 v3.0                ║")
    logger.info("╚══════════════════════════════════════════════╝")
    logger.info(f"代理: {PROXY_URL}")
    logger.info(f"轮询间隔: {POLL_INTERVAL_SECONDS}s")
    logger.info(f"数据源: {len(ALL_SOURCES)} 个")
    logger.info(f"并发数: {MAX_CONCURRENT_FETCH}")

    # 初始化数据库
    init_db()

    # 主循环
    while not _shutdown_requested:
        _global_cycle_count += 1
        cycle_start = time.time()

        try:
            stats = run_poll_cycle()

            # 日志汇总
            logger.info(
                f"📋 第 {_global_cycle_count} 轮完成: "
                f"抓取={stats['fetched']}, 翻译={stats['translated']}, "
                f"新增={stats['new']}, 耗时={stats['duration_seconds']:.1f}s"
            )
            if stats.get("errors"):
                logger.warning(f"⚠️ 本轮错误: {stats['errors']}")

        except Exception as e:
            logger.error(f"❌ 轮询异常: {e}")
            logger.error(traceback.format_exc())

        # 等待下一轮
        elapsed = time.time() - cycle_start
        wait = max(1, POLL_INTERVAL_SECONDS - elapsed)

        if _shutdown_requested:
            break

        logger.info(f"⏳ 等待 {wait:.0f}s 后开始下一轮...")
        # 分段 sleep 以响应退出信号
        for _ in range(int(wait)):
            if _shutdown_requested:
                break
            time.sleep(1)

    logger.info("🛑 轮询系统已优雅退出")
    logger.info(f"📊 总计执行 {_global_cycle_count} 轮")


if __name__ == "__main__":
    main()
