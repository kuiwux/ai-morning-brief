#!/usr/bin/env python3
"""
硅谷AI晨报 · 公众号 HTML 渲染器
读取 output.json + wechat_template.html → 输出 output_wechat.html
"""

import os
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

WORKDIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(WORKDIR, "templates", "wechat_template.html")
# 回退：如果模板不在当前目录，检查 pipeline/ 子目录
if not os.path.exists(TEMPLATE_PATH):
    alt_template = os.path.join(WORKDIR, "pipeline", "templates", "wechat_template.html")
    if os.path.exists(alt_template):
        TEMPLATE_PATH = alt_template
DEFAULT_JSON = os.path.join(WORKDIR, "output.json")
DEFAULT_OUTPUT = os.path.join(WORKDIR, "output_wechat.html")


def _read_template() -> str:
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _load_data(json_path: str) -> dict:
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _format_date(date_str: str) -> str:
    """将日期字符串转为中文格式，如 '2026-05-29' → '2026年5月29日 星期五'"""
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    try:
        if "-" in date_str:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return f"{dt.year}年{dt.month}月{dt.day}日 {weekdays[dt.weekday()]}"
    except ValueError:
        pass
    return date_str


def _format_weekday(date_str: str) -> str:
    """返回星期几"""
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    try:
        if "-" in date_str:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return weekdays[dt.weekday()]
    except ValueError:
        pass
    return ""


def _extract_domain(url: str) -> str:
    """从 URL 提取域名/来源名"""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        domain = domain.replace("www.", "")
        # 常见中文映射
        domain_map = {
            "news.ycombinator.com": "HN",
            "github.com": "GitHub",
            "techcrunch.com": "TechCrunch",
            "theverge.com": "The Verge",
            "reuters.com": "Reuters",
            "bloomberg.com": "Bloomberg",
            "arstechnica.com": "Ars Technica",
            "wired.com": "Wired",
            "venturebeat.com": "VentureBeat",
            "theinformation.com": "The Information",
            "nitter.net": "X/Twitter",
            "arxiv.org": "ArXiv",
            "huggingface.co": "HuggingFace",
            "youtube.com": "YouTube",
            "openai.com": "OpenAI",
            "anthropic.com": "Anthropic",
            "deepmind.google": "DeepMind",
            "meta.com": "Meta",
            "nvidia.com": "NVIDIA",
            "amazon.com": "Amazon",
            "microsoft.com": "Microsoft",
            "google.com": "Google",
        }
        for key, val in domain_map.items():
            if key in domain:
                return val
        return domain.split(".")[0].capitalize()
    except Exception:
        return ""


def _article_source_tags(article: dict) -> str:
    """从文章数据提取来源标签 HTML"""
    tags = article.get("source_tags", [])
    if not tags:
        # fallback: 使用 source_type + URL 域名
        source_type = article.get("source_type", "")
        type_map = {"hn": "HN", "arxiv": "ArXiv", "hf": "HuggingFace", "x": "X/Twitter", "yt": "YouTube"}
        tag = type_map.get(source_type, source_type.upper())
        domain = _extract_domain(article.get("url", ""))
        if domain and domain != tag:
            tags = [domain, tag]
        else:
            tags = [tag]

    parts = []
    for t in tags:
        parts.append(f'<span style="color:#A68A6B;text-decoration:none;">{t}</span>')
    return " · ".join(parts)


def _build_headline_section(data: dict) -> str:
    """构建今日头条 HTML"""
    headline = data.get("headline", {})
    if isinstance(headline, str):
        title = ""
        summary = headline
        sources_html = ""
    else:
        title = headline.get("title", "") or headline.get("summary", "")[:30]
        summary = headline.get("summary", "")
        sources = headline.get("sources", [])
        if sources:
            sources_html = "<p style=\"margin:0;font-size:12px;color:#A68A6B;line-height:1.6;\">📰 " + " · ".join(
                f'<span style=\"color:#8B6914;\">{s}</span>' for s in sources
            ) + "</p>"
        else:
            sources_html = ""

    return f"""
    <!-- 板块标签 -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td>
        <span style="display:inline-block;background-color:#8B4513;color:#FFFFFF;font-size:12px;font-weight:700;padding:3px 12px;border-radius:2px;letter-spacing:2px;margin-bottom:12px;">
          今 日 头 条
        </span>
      </td>
    </tr>
    </table>

    <!-- 头条卡片容器 -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FFF7ED;border:1px solid #E8D5B7;border-radius:4px;">
    <tr>
      <td style="padding:16px;">

        <!-- 头条标题 -->
        <h2 style="margin:0 0 10px 0;font-family:'Songti SC','SimSun',serif;font-size:18px;font-weight:700;color:#5C3317;line-height:1.5;">
          {title}
        </h2>

        <!-- 摘要 -->
        <p style="margin:0 0 12px 0;font-size:15px;color:#5C4A3A;line-height:1.8;text-align:justify;">
          {summary}
        </p>

        <!-- 来源标注 -->
        {sources_html}

      </td>
    </tr>
    </table>"""


def _build_articles_section(data: dict) -> str:
    """构建 AI 快讯列表 HTML，按分类分组，去重头条"""
    articles = data.get("articles", [])
    if not articles:
        return ""

    # 获取头条标题用于去重
    headline = data.get("headline", {})
    if isinstance(headline, dict):
        headline_title = headline.get("title", "")
    else:
        headline_title = ""

    # 按分类分组，同时过滤掉与头条重复的
    groups: dict[str, list[dict]] = {}
    for art in articles:
        title = art.get("title", "")
        # 跳过与头条标题重复的文章
        if headline_title and title and (
            title in headline_title or headline_title in title
        ):
            continue
        cat = art.get("category", "其他")
        if cat not in groups:
            groups[cat] = []
        groups[cat].append(art)

    # 保持 articles 中分类首次出现顺序
    seen_cats = []
    for art in articles:
        cat = art.get("category", "其他")
        if cat not in seen_cats and cat in groups and groups[cat]:
            seen_cats.append(cat)

    if not seen_cats:
        return ""

    CAT_ICONS = {
        "公司动态": "🏢",
        "技术突破": "🚀",
        "产品发布": "📦",
        "观点评论": "💬",
        "学术前沿": "📖",
    }

    sections_html = ""
    first = True
    for cat in seen_cats:
        cat_articles = groups[cat]
        icon = CAT_ICONS.get(cat, "📌")

        border_html = ""
        if not first:
            border_html = 'style="padding-top:12px;border-top:1px dashed #E0D0B8;"'
        first = False

        items_html = ""
        for art in cat_articles:
            title = art.get("title", "")
            summary = art.get("summary", "")
            # 人物/组织标签
            person_tags = art.get("tags", []) or art.get("persons", [])
            tags_html = ""
            if person_tags:
                tag_spans = " · ".join(
                    f'<span style="display:inline-block;background:#F0E6D3;color:#8B6914;font-size:11px;padding:1px 8px;border-radius:10px;margin:2px 2px;">{t}</span>'
                    for t in person_tags[:3]
                )
                tags_html = f'<p style="margin:6px 0 0 0;">{tag_spans}</p>'

            items_html += f"""
    <!-- 简讯 -->
    <div style="margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid #F0E6D3;">
      <p style="margin:0;font-size:14px;font-weight:600;color:#5C3317;line-height:1.7;">
        {title}
      </p>
      <p style="margin:4px 0 0 0;font-size:13px;color:#6B5B4F;line-height:1.7;">
        {summary}
      </p>
      {tags_html}
    </div>"""

        sections_html += f"""
    <!-- ══ 主题分组：{cat} ══ -->
    <div {border_html}>
      <p style="margin:0 0 10px 0;font-size:14px;font-weight:700;color:#8B4513;">
        {icon} {cat}
      </p>
      {items_html}
    </div>"""

    return f"""
    <!-- 板块标签 -->
    <div style="margin-bottom:14px;">
      <span style="display:inline-block;background-color:#8B6914;color:#FFFFFF;font-size:12px;font-weight:700;padding:3px 12px;border-radius:2px;letter-spacing:2px;">
        AI 快 讯
      </span>
    </div>
    {sections_html}"""


def _build_eli5_section(data: dict) -> str:
    """构建 ELI5 通俗解释区域"""
    articles = data.get("articles", [])
    headline = data.get("headline", {})

    eli5_entries = []

    # 头条的 ELI5
    if isinstance(headline, dict) and headline.get("eli5"):
        eli5_entries.append({
            "title": f"🧠 {headline.get('title', '今日头条')[:25]}",
            "text": headline["eli5"],
        })

    # 各文章的 ELI5
    for art in articles:
        eli5_text = art.get("eli5", "")
        if eli5_text:
            title = art.get("title", "")[:25]
            eli5_entries.append({
                "title": f"💡 {title}",
                "text": eli5_text,
            })

    if not eli5_entries:
        return ""

    items_html = ""
    for item in eli5_entries[:5]:  # 最多5条
        items_html += f"""
    <!-- ELI5 条目 -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FFF9F0;border-left:4px solid #E8A650;margin-bottom:14px;border-radius:0 4px 4px 0;">
    <tr>
      <td style="padding:14px 16px;">
        <p style="margin:0 0 8px 0;font-size:14px;font-weight:700;color:#8B4513;">
          {item['title']}
        </p>
        <p style="margin:0;font-size:14px;color:#5C4A3A;line-height:1.8;">
          {item['text']}
        </p>
      </td>
    </tr>
    </table>"""

    return f"""
    <!-- 板块标签 -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td>
        <span style="display:inline-block;background-color:#D2691E;color:#FFFFFF;font-size:12px;font-weight:700;padding:3px 12px;border-radius:2px;letter-spacing:2px;margin-bottom:14px;">
          💡 今日 ELI5
        </span>
      </td>
    </tr>
    </table>

    <p style="margin:0 0 14px 0;font-size:13px;color:#A68A6B;line-height:1.6;">
      用大白话解释今天最重要的 AI 新闻
    </p>
    {items_html}"""


def _build_featured_topic_section(data: dict) -> str:
    """构建编辑精选专题 HTML"""
    topic = data.get("featured_topic")
    if not topic or not isinstance(topic, dict):
        return ""

    title = topic.get("title", "")
    summary = topic.get("summary", "")
    article_ids = topic.get("articles", [])
    article_count = len(article_ids)

    # 专题链接：优先使用 topic 中的 url 字段，否则使用默认专题页
    topic_url = topic.get("url", "") or "http://172.22.39.187:8899/topic"

    # 计算涉及的来源数
    articles = data.get("articles", [])
    source_types = set()
    for art in articles:
        if art.get("id") in article_ids:
            st = art.get("source_type", "")
            if st:
                source_types.add(st)

    return f"""
    <!-- 板块标签 -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td>
        <span style="display:inline-block;background-color:#5C3317;color:#F5DEB3;font-size:12px;font-weight:700;padding:3px 12px;border-radius:2px;letter-spacing:2px;margin-bottom:14px;">
          📖 编辑精选专题
        </span>
      </td>
    </tr>
    </table>

    <!-- 专题卡片 -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:linear-gradient(135deg, #FFF7ED 0%, #FFF0E0 100%);border:1px solid #E0C8A0;border-radius:6px;">
    <tr>
      <td style="padding:18px;">

        <!-- 专题标题 -->
        <h3 style="margin:0 0 8px 0;font-family:'Songti SC','SimSun',serif;font-size:16px;font-weight:700;color:#5C3317;line-height:1.5;">
          🔥 {title}
        </h3>

        <!-- 专题摘要 -->
        <p style="margin:0 0 12px 0;font-size:13px;color:#6B5B4F;line-height:1.8;">
          {summary}
        </p>

        <!-- 专题元信息 -->
        <p style="margin:0;font-size:11px;color:#A68A6B;">
          📊 收录 {article_count} 篇深度报道 · 📰 {len(source_types)} 个独立来源
        </p>

        <!-- CTA 按钮 -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td style="padding-top:12px;text-align:center;">
            <a href="{topic_url}" style="display:inline-block;background-color:#8B4513;color:#FFFFFF;font-size:13px;font-weight:600;text-decoration:none;padding:8px 32px;border-radius:3px;letter-spacing:1px;">
              查看完整专题 →
            </a>
          </td>
        </tr>
        </table>

      </td>
    </tr>
    </table>"""


def _build_data_section(data: dict) -> str:
    """构建底部数据一览"""
    meta = data.get("meta", {})
    article_count = meta.get("article_count", len(data.get("articles", [])))
    source_count = meta.get("source_count", 5)

    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FAF5EF;border:1px solid #E8D5B7;border-radius:4px;">
    <tr>
      <td style="padding:14px 16px;text-align:center;">

        <p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:#8B6914;">
          📊 今日数据一览
        </p>

        <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td width="33%" align="center" style="padding:4px 0;">
            <p style="margin:0;font-size:18px;font-weight:700;color:#8B4513;">{article_count}</p>
            <p style="margin:0;font-size:11px;color:#A68A6B;">收录文章</p>
          </td>
          <td width="33%" align="center" style="padding:4px 0;">
            <p style="margin:0;font-size:18px;font-weight:700;color:#8B4513;">{source_count}</p>
            <p style="margin:0;font-size:11px;color:#A68A6B;">数据来源</p>
          </td>
          <td width="33%" align="center" style="padding:4px 0;">
            <p style="margin:0;font-size:14px;font-weight:700;color:#D2691E;">读者好评</p>
            <p style="margin:0;font-size:11px;color:#A68A6B;">品质指数</p>
          </td>
        </tr>
        </table>

      </td>
    </tr>
    </table>"""


def render(json_path: str = None, output_path: str = None) -> str:
    """
    渲染公众号 HTML。

    Args:
        json_path: JSON 数据文件路径，默认 output.json
        output_path: 输出 HTML 文件路径，默认 output_wechat.html

    Returns:
        渲染后的 HTML 字符串
    """
    json_path = json_path or os.path.join(WORKDIR, "output.json")
    output_path = output_path or DEFAULT_OUTPUT

    # 如果 json_path 不存在，尝试用日期化文件
    if not os.path.exists(json_path):
        # 尝试找最新的 output_*.json
        import glob
        pattern = os.path.join(WORKDIR, "output_*.json")
        files = glob.glob(pattern)
        if files:
            files.sort(reverse=True)
            json_path = files[0]
            print(f"⚠️ 使用最新数据文件: {json_path}")

    if not os.path.exists(json_path):
        raise FileNotFoundError(f"未找到 JSON 数据文件: {json_path}")

    # 加载数据
    data = _load_data(json_path)
    meta = data.get("meta", {})

    # 加载模板
    html = _read_template()

    # ---- 替换封面图 URL（如果有的话） ----
    date_str = meta.get("date", "")
    if date_str:
        cover_url = f"https://your-cdn.com/covers/{date_str.replace('-', '')}_main.jpg"
        html = html.replace("https://your-cdn.com/covers/20260529_main.jpg", cover_url)

    # ---- 刊头标题：用头条标题缩写 ----
    headline = data.get("headline", {})
    if isinstance(headline, dict):
        headline_title = headline.get("title", "") or headline.get("summary", "")[:40]
    else:
        headline_title = str(headline)[:40] if headline else ""
    if not headline_title:
        headline_title = "硅谷AI晨报"
    html = html.replace("{{HEADLINE_TITLE}}", headline_title)

    # ---- 日期 ----
    formatted_date = _format_date(date_str) if date_str else ""
    html = html.replace("{{DATE_FORMATTED}}", formatted_date)

    # ---- 移除旧版日期替换（已迁移到模板变量） ----
    html = html.replace("📅 2026年5月29日 星期五", "")
    html = html.replace("📰 5 数据源 · ⏱ 8 分钟阅读", "")

    # ---- 替换头条区域 ----
    headline_html = _build_headline_section(data)
    # 找到头条卡片的起止标记并替换
    headline_start = html.find("<!-- 板块标签 -->")
    template_start = html.find("<!-- 模块3：今日头条卡片                                      -->")
    articles_start = html.find("<!-- ════════════════════════════════════════════════════════ -->", html.find("模块4"))
    if articles_start == -1:
        articles_start = html.find("<!-- 模块4：AI 快讯")
    if articles_start == -1:
        # fallback: find the decoration divider after headline
        articles_start = html.find("<!-- 装饰分割线 -->")

    if template_start != -1 and articles_start != -1:
        # 找到模块3开始到下一个模块
        module3_start = html.find('<tr>', template_start)
        # 找到包含头条卡片的下一个模板的边界：装饰分割线前的最后一个 </td></tr></table>
        end_marker = html.find("<!-- 装饰分割线 -->", module3_start)
        if end_marker != -1:
            # 替换从模块3的 <tr> 到装饰分割线之间的内容
            before = html[:module3_start]
            after = html[end_marker:]
            html = before + headline_html + "\n\n" + after

    # ---- 替换 AI 快讯列表 ----
    articles_html = _build_articles_section(data)
    articles_start = html.find("<!-- 模块4：AI 快讯 列表")
    if articles_start == -1:
        articles_start = html.find("<!-- 模块4：AI 快讯")
    eli5_start = html.find("<!-- 模块5：今日 ELI5")
    if eli5_start == -1:
        eli5_start = html.find("<!-- 模块5：")

    if articles_start != -1 and eli5_start != -1:
        # Find the opening <tr> after module4 marker
        tr_start = html.find("<tr>", articles_start)
        # Find the closing </td></tr> before module5, go back to find the actual end
        section_end = html.rfind("</td>", articles_start, eli5_start)
        tr_end = html.rfind("</tr>", articles_start, eli5_start)

        # The section spans from the first <tr> after module4 to the last </tr> before module5
        # Actually, let's find the actual container boundaries more reliably
        container_start = tr_start
        container_end = html.rfind("</table>", articles_start, eli5_start)
        # look for the closing of the news list
        # The news list section is inside a <td style="padding:0 20px 20px 20px;"> block
        td_start = html.find("<td", container_start)
        # Find matching </td>
        td_depth = 0
        td_end = td_start
        for i in range(td_start, eli5_start):
            tag = html[i:i+4]
            if tag == "<td " or tag == "<td>":
                td_depth += 1
            elif tag == "</td":
                td_depth -= 1
                if td_depth == 0:
                    td_end = i + 5
                    break

        if td_end > td_start:
            # Build the section
            news_section = f"""<tr>
  <td style="padding:0 20px 20px 20px;">
    {articles_html}
  </td>
</tr>"""
            html = html[:container_start] + news_section + "\n\n" + html[td_end:]

    # ---- 替换 ELI5 区域 ----
    eli5_html = _build_eli5_section(data)
    eli5_start = html.find("<!-- 模块5：今日 ELI5")
    if eli5_start == -1:
        eli5_start = html.find("<!-- 模块5：")
    featured_start = html.find("<!-- 模块6：编辑精选专题")

    if eli5_start != -1 and featured_start != -1 and eli5_html:
        tr_start = html.find("<tr>", eli5_start)
        td_end = html.rfind("</td>", eli5_start, featured_start)
        tr_end = html.rfind("</tr>", eli5_start, featured_start)
        # Find the full block
        section_tr_start = tr_start
        section_tr_end = html.rfind("</tr>", eli5_start, featured_start) + 5

        eli5_section = f"""<tr>
  <td style="padding:18px 20px 8px 20px;">
    {eli5_html}
  </td>
</tr>"""
        html = html[:section_tr_start] + eli5_section + "\n\n" + html[section_tr_end:]

    # ---- 替换编辑精选专题 ----
    featured_html = _build_featured_topic_section(data)
    featured_start = html.find("<!-- 模块6：编辑精选专题")
    if featured_start == -1:
        featured_start = html.find("<!-- 模块6：")
    data_section_start = html.find("<!-- 模块7：数据一览")

    if featured_start != -1 and data_section_start != -1:
        tr_start = html.find("<tr>", featured_start)
        # Find the closing before 模块7
        section_tr_end = html.rfind("</tr>", featured_start, data_section_start) + 5

        if featured_html:
            featured_section = f"""<tr>
  <td style="padding:18px 20px;">
    {featured_html}
  </td>
</tr>"""
        else:
            featured_section = ""

        html = html[:tr_start] + featured_section + "\n\n" + html[section_tr_end:]

    # ---- 替换数据一览（公众号版已移除） ----
    # data 模块不再渲染，跳过即可

    # ---- 写入输出 ----
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ 公众号 HTML 已渲染: {output_path}")
    return html


if __name__ == "__main__":
    import sys
    json_path = sys.argv[1] if len(sys.argv) > 1 else None
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    render(json_path=json_path, output_path=output_path)
