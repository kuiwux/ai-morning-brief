#!/usr/bin/env python3
"""
AI 对话核心服务 — 对接 DeepSeek API，支持三种对话模式
"""
import os
import sys
import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from openai import OpenAI

# 复用现有 pipeline 中的客户端和 model 配置
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import get_deepseek_client, DEEPSEEK_MODEL

from chat_db import (
    save_chat,
    get_chat_history,
    clear_chat_history,
    check_daily_quota,
    increment_daily_quota,
    FREE_DAILY_LIMIT,
)

logger = logging.getLogger("chat_service")

# ── 数据库导入 ─────────────────────────────────────────────────────────────
from database import get_article_by_id, get_articles  # noqa: E402

# ── 配置 ────────────────────────────────────────────────────────────────────
MAX_CONTEXT_TOKENS = 8000  # 上下文窗口上限（估算）
RESPONSE_TIMEOUT = 30  # DeepSeek API 超时秒数
SYSTEM_PROMPT_BASE = (
    "你是一位资深的硅谷AI行业分析师，专注于人工智能、机器学习、科技创投领域。"
    "你的回答应当专业、简洁、有深度，用中文回复。"
    "如果问题超出给定资料范围，可以结合你的专业知识进行扩展讨论，但要明确说明哪些是你的推断。"
)


class ChatService:
    """AI 对话服务员"""

    def __init__(self):
        self.deepseek_client = get_deepseek_client()

    # ── 核心对话接口 ─────────────────────────────────────────────────────

    async def chat(
        self,
        user_id: str,
        question: str,
        article_id: Optional[str] = None,
        mode: str = "article",
    ) -> dict:
        """
        核心对话方法

        Args:
            user_id: 用户 ID
            question: 用户问题
            article_id: 文章 ID（模式1：文章追问时传入）
            mode: 对话模式
                - "article": 基于单篇文章追问
                - "daily_summary": 每日总结
                - "trend": 趋势分析（近7天）

        Returns:
            {
                "reply": str,           # AI 回复
                "tokens_used": int,     # token 消耗
                "remaining_queries": int, # 剩余查询次数
                "mode": str,            # 使用的模式
            }
            或错误:
            {
                "reply": null,
                "error": str,
                "remaining_queries": int,
            }
        """
        # 1. 检查每日配额
        quota = check_daily_quota(user_id)
        if not quota["can_query"]:
            return {
                "reply": None,
                "error": "今日AI追问次数已用完，升级Pro享无限对话",
                "remaining_queries": 0,
            }

        # 2. 构建 system prompt
        try:
            system_prompt = await self._build_system_prompt(article_id, mode)
        except Exception as e:
            logger.error(f"构建 system prompt 失败: {e}")
            system_prompt = SYSTEM_PROMPT_BASE

        # 3. 调用 DeepSeek
        try:
            reply, tokens_used = await self._call_deepseek(system_prompt, question)
        except Exception as e:
            logger.error(f"DeepSeek API 调用失败: {e}")
            return {
                "reply": None,
                "error": "AI 服务暂时不可用，请稍后重试",
                "remaining_queries": quota["remaining"],
            }

        # 4. 消耗配额
        increment_daily_quota(user_id)
        new_quota = check_daily_quota(user_id)

        # 5. 保存对话历史
        save_chat(
            user_id=user_id,
            question=question,
            answer=reply,
            article_id=article_id,
            mode=mode,
            tokens_used=tokens_used,
        )

        return {
            "reply": reply,
            "tokens_used": tokens_used,
            "remaining_queries": new_quota["remaining"],
            "mode": mode,
        }

    # ── 对话历史 ─────────────────────────────────────────────────────────

    async def get_history(
        self,
        user_id: str,
        article_id: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """获取对话历史"""
        return get_chat_history(user_id, article_id, limit)

    async def clear_history(
        self,
        user_id: str,
        article_id: Optional[str] = None,
    ) -> int:
        """清除对话历史，返回删除条数"""
        return clear_chat_history(user_id, article_id)

    # ── System Prompt 构建 ───────────────────────────────────────────────

    async def _build_system_prompt(
        self, article_id: Optional[str], mode: str
    ) -> str:
        """根据模式构建 system prompt"""
        if mode == "article":
            return await self._build_article_prompt(article_id)
        elif mode == "daily_summary":
            return await self._build_daily_prompt()
        elif mode == "trend":
            return await self._build_trend_prompt()
        else:
            return SYSTEM_PROMPT_BASE

    async def _build_article_prompt(self, article_id: Optional[str]) -> str:
        """模式1：基于单篇文章追问"""
        if not article_id:
            return SYSTEM_PROMPT_BASE

        article = get_article_by_id(article_id)
        if not article:
            logger.warning(f"文章不存在: {article_id}")
            return SYSTEM_PROMPT_BASE

        title = article.get("title", "未知标题")
        title_cn = article.get("title_cn", "")
        summary = article.get("summary", "暂无摘要")
        summary_cn = article.get("summary_cn", "")
        ai_comment = article.get("ai_comment", "")
        category = article.get("category", "")
        tags = article.get("tags", [])
        source_region = article.get("source_region", "")

        prompt_parts = [
            SYSTEM_PROMPT_BASE,
            "",
            "── 当前文章 ──",
            f"标题（原文）：{title}",
        ]

        if title_cn:
            prompt_parts.append(f"标题（中文）：{title_cn}")

        prompt_parts.append(f"分类：{category or '未分类'}")
        prompt_parts.append(f"来源地区：{source_region or '未知'}")

        if tags:
            prompt_parts.append(f"标签：{', '.join(tags) if isinstance(tags, list) else tags}")

        prompt_parts.append("")
        prompt_parts.append(f"摘要（原文）：{summary}")
        if summary_cn:
            prompt_parts.append(f"摘要（中文）：{summary_cn}")

        if ai_comment:
            prompt_parts.append("")
            prompt_parts.append(f"📌 AI 点评：{ai_comment}")

        prompt_parts.append("")
        prompt_parts.append("请基于以上文章内容回答读者的问题。如果问题超出文章范围，可以结合你的专业知识扩展讨论，但要注明哪些内容是你的补充分析。")

        return "\n".join(prompt_parts)

    async def _build_daily_prompt(self) -> str:
        """模式2：每日总结 — 注入今日所有文章标题"""
        today = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
        articles, _ = get_articles(since=f"{today}T00:00:00", limit=50)

        if not articles:
            return SYSTEM_PROMPT_BASE + "\n\n今日暂无收录文章。"

        prompt_parts = [
            SYSTEM_PROMPT_BASE,
            "",
            f"── 今日（{today}）收录文章 ──",
        ]

        for i, a in enumerate(articles, 1):
            title = a.get("title", "无标题")
            title_cn = a.get("title_cn", "")
            category = a.get("category", "")
            display = title_cn or title
            prompt_parts.append(f"{i}. [{category}] {display}")

        prompt_parts.append("")
        prompt_parts.append(
            "请基于以上今日文章列表，回答读者的问题。读者通常会问："
            "'今天最重要的3件事是什么？'、'今天有什么值得关注的？'、"
            "'给我今天的AI圈精华总结'等。请用结构化方式回复，每条包含："
            "标题、要点、为什么重要。控制在500字以内。"
        )

        return "\n".join(prompt_parts)

    async def _build_trend_prompt(self) -> str:
        """模式3：趋势分析 — 注入近7天文章"""
        beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
        seven_days_ago = (beijing_now - timedelta(days=7)).strftime("%Y-%m-%d")

        articles, total = get_articles(since=f"{seven_days_ago}T00:00:00", limit=100)

        if not articles:
            prompt = SYSTEM_PROMPT_BASE + f"\n\n近7天（{seven_days_ago} 至今）暂无收录文章，无法提供趋势分析。"
            return prompt

        prompt_parts = [
            SYSTEM_PROMPT_BASE,
            "",
            f"── 近7天（{seven_days_ago} → {beijing_now.strftime('%m月%d日')}）趋势分析 ──",
            f"共计 {total} 篇文章，以下是代表性标题：",
            "",
        ]

        # 只取前 50 条做标题列表，避免 prompt 过长
        for i, a in enumerate(articles[:50], 1):
            title = a.get("title", "无标题")
            title_cn = a.get("title_cn", "")
            category = a.get("category", "")
            display = title_cn or title
            created = a.get("created_at", "")
            date_str = created[:10] if created else ""
            prompt_parts.append(f"{i}. [{date_str}][{category}] {display}")

        prompt_parts.append("")
        prompt_parts.append(
            "请基于以上近7天的文章数据，分析本周AI圈的趋势：\n"
            "1. 哪些话题在持续升温？\n"
            "2. 出现了哪些新的方向或突破？\n"
            "3. 本周最值得关注的事件是什么？\n"
            "4. 未来可能的发展方向？\n"
            "请用结构化分析，控制在800字以内。"
        )

        return "\n".join(prompt_parts)

    # ── DeepSeek API 调用 ────────────────────────────────────────────────

    async def _call_deepseek(self, system_prompt: str, question: str) -> tuple[str, int]:
        """
        调用 DeepSeek API
        返回 (reply, tokens_used)
        """
        # 截断过长的 system prompt（预留 max_tokens 空间）
        max_prompt_chars = MAX_CONTEXT_TOKENS * 3  # 粗略估计：1 token ≈ 3 字符（中文）
        if len(system_prompt) > max_prompt_chars:
            system_prompt = system_prompt[:max_prompt_chars] + "\n...（内容过长，已截断）"

        response = self.deepseek_client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            temperature=0.7,
            max_tokens=2000,
            timeout=RESPONSE_TIMEOUT,
        )

        reply = response.choices[0].message.content
        tokens_used = response.usage.total_tokens if response.usage else 0

        logger.info(f"DeepSeek 返回 {len(reply)} 字符，消耗 {tokens_used} tokens")
        return reply, tokens_used


# ── 全局单例 ────────────────────────────────────────────────────────────────
_chat_service: Optional[ChatService] = None


def get_chat_service() -> ChatService:
    """获取 ChatService 单例"""
    global _chat_service
    if _chat_service is None:
        _chat_service = ChatService()
    return _chat_service
