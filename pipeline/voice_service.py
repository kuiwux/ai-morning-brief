"""
语音克隆和 TTS 服务
主引擎：Fish Audio API（语音克隆 + TTS）
兜底引擎：Edge TTS（预设语音，免费）
"""

import os
import io
import sys
import time
import uuid
import base64
import logging
import asyncio
import tempfile
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── 代理配置 ────────────────────────────────────────────────────────────────
PROXY_URL = "http://172.23.80.1:7890"

# ── Fish Audio 预设语音 ─────────────────────────────────────────────────────
# Edge TTS 预设语音映射
EDGE_VOICE_MAP = {
    "edge_yunxi":    {"voice": "zh-CN-YunxiNeural",     "name": "男播音员", "emoji": "🧑", "style": "沉稳大气"},
    "edge_xiaoxiao": {"voice": "zh-CN-XiaoxiaoNeural",  "name": "女播音员", "emoji": "👩", "style": "温柔清晰"},
    "edge_yunjian":  {"voice": "zh-CN-YunjianNeural",   "name": "电影解说", "emoji": "🎬", "style": "戏谑幽默"},
    "edge_yunyang":  {"voice": "zh-CN-YunyangNeural",   "name": "技术极客", "emoji": "🤖", "style": "快速理性"},
}


class VoiceService:
    """语音克隆和 TTS 服务"""

    def __init__(self):
        self.fish_api_key = os.environ.get("FISH_AUDIO_API_KEY", "")
        self.fish_base_url = "https://api.fish.audio"
        self._demon_mode = not self.fish_api_key  # 演示模式 = 无 API Key

    # ═══════════════════════════════════════════════════════════════════════
    # Fish Audio API
    # ═══════════════════════════════════════════════════════════════════════

    async def clone_voice(
        self,
        audio_path: str,
        name: str,
        description: str = "",
    ) -> dict:
        """
        上传音频文件到 Fish Audio 进行语音克隆。
        
        Args:
            audio_path: 音频文件路径（本地文件）
            name: 语音名称
            description: 语音描述
            
        Returns:
            dict: {voice_id, name, status, preview_url}
        """
        if self._demon_mode:
            return self._demo_clone(name)

        # 验证文件
        if not os.path.isfile(audio_path):
            return {"error": f"音频文件不存在: {audio_path}", "status": "failed"}

        file_size = os.path.getsize(audio_path)
        max_size = 10 * 1024 * 1024  # 10 MB
        if file_size > max_size:
            return {"error": f"文件过大（{file_size} bytes），最大支持 10MB", "status": "failed"}

        ext = os.path.splitext(audio_path)[1].lower().lstrip(".")
        allowed_exts = {"mp3", "wav", "m4a", "mp4", "mov", "flac", "ogg", "aac", "wma"}
        if ext not in allowed_exts:
            return {
                "error": f"不支持的文件格式: .{ext}，支持: {', '.join(sorted(allowed_exts))}",
                "status": "failed",
            }

        try:
            # 生成唯一文件名
            unique_name = f"{uuid.uuid4().hex[:8]}_{os.path.basename(audio_path)}"

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0),
                proxy=PROXY_URL,
            ) as client:
                # Step 1: 上传音频文件
                with open(audio_path, "rb") as f:
                    files = {
                        "file": (unique_name, f, f"audio/{ext if ext != 'mp3' else 'mpeg'}"),
                    }
                    form_data = {
                        "name": name,
                        "description": description or f"Cloned voice: {name}",
                    }

                    upload_resp = await client.post(
                        f"{self.fish_base_url}/v1/voice-model",
                        headers={
                            "Authorization": f"Bearer {self.fish_api_key}",
                        },
                        files=files,
                        data=form_data,
                    )

                if upload_resp.status_code not in (200, 201):
                    logger.error(f"Fish Audio 上传失败: {upload_resp.status_code} {upload_resp.text}")
                    return {
                        "error": f"Fish Audio 上传失败 (HTTP {upload_resp.status_code})",
                        "detail": upload_resp.text[:500],
                        "status": "failed",
                    }

                result = upload_resp.json()
                voice_id = result.get("id") or result.get("voice_id") or result.get("model_id")

                if not voice_id:
                    logger.warning(f"Fish Audio 响应无 voice_id: {result}")
                    # 尝试轮询获取
                    voice_id = result.get("_id")

                if not voice_id:
                    return {
                        "error": "Fish Audio 返回数据异常，无法获取 voice_id",
                        "raw_response": result,
                        "status": "failed",
                    }

                # Step 2: 轮询等待训练完成
                status = result.get("status", "training")
                preview_url = result.get("preview_url", "")

                if status in ("pending", "training", "processing"):
                    logger.info(f"语音 {voice_id} 正在训练，等待完成...")
                    ready = await self._poll_voice_status(client, voice_id, max_wait=60.0)
                    if ready:
                        status = "ready"
                    else:
                        status = "training"  # 仍在训练，但不等了

                return {
                    "voice_id": voice_id,
                    "name": name,
                    "status": status,
                    "preview_url": preview_url,
                }

        except httpx.ConnectError as e:
            logger.error(f"无法连接 Fish Audio API（代理问题？）: {e}")
            return {"error": f"网络连接失败: {e}", "status": "failed"}
        except Exception as e:
            logger.error(f"Fish Audio 克隆异常: {e}", exc_info=True)
            return {"error": str(e), "status": "failed"}

    async def _poll_voice_status(
        self,
        client: httpx.AsyncClient,
        voice_id: str,
        max_wait: float = 60.0,
        interval: float = 3.0,
    ) -> bool:
        """轮询语音训练状态，返回 True 表示就绪"""
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                resp = await client.get(
                    f"{self.fish_base_url}/v1/voice-model/{voice_id}",
                    headers={"Authorization": f"Bearer {self.fish_api_key}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status", "")
                    if status in ("ready", "completed", "success"):
                        return True
                    elif status in ("failed", "error"):
                        logger.error(f"语音训练失败: {data}")
                        return False
            except Exception as e:
                logger.warning(f"轮询状态异常: {e}")

            await asyncio.sleep(interval)

        logger.warning(f"语音 {voice_id} 超时未就绪")
        return False

    async def tts_generate(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
    ) -> Optional[bytes]:
        """
        使用 Fish Audio 生成 TTS 音频。

        Args:
            text: 要合成的文本
            voice_id: Fish Audio 语音 ID
            speed: 语速（0.5 ~ 2.0）

        Returns:
            bytes: mp3 音频数据，失败返回 None
        """
        if self._demon_mode or not self.fish_api_key:
            return None

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0),
                proxy=PROXY_URL,
            ) as client:
                resp = await client.post(
                    f"{self.fish_base_url}/v1/tts",
                    headers={
                        "Authorization": f"Bearer {self.fish_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "text": text,
                        "voice_id": voice_id,
                        "speed": max(0.5, min(2.0, speed)),
                    },
                )

                if resp.status_code == 200:
                    return resp.content
                else:
                    logger.error(f"Fish Audio TTS 失败: {resp.status_code} {resp.text[:300]}")
                    return None

        except Exception as e:
            logger.error(f"Fish Audio TTS 异常: {e}")
            return None

    async def delete_voice(self, voice_id: str) -> bool:
        """删除 Fish Audio 上的克隆语音"""
        if self._demon_mode:
            return True

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                proxy=PROXY_URL,
            ) as client:
                resp = await client.delete(
                    f"{self.fish_base_url}/v1/voice-model/{voice_id}",
                    headers={"Authorization": f"Bearer {self.fish_api_key}"},
                )
                return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"删除语音失败: {e}")
            return False

    async def get_fish_voices(self) -> list[dict]:
        """获取 Fish Audio 上的语音列表"""
        if self._demon_mode:
            return []

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                proxy=PROXY_URL,
            ) as client:
                resp = await client.get(
                    f"{self.fish_base_url}/v1/voice-model",
                    headers={"Authorization": f"Bearer {self.fish_api_key}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    items = data if isinstance(data, list) else data.get("items", [])
                    return [
                        {
                            "voice_id": item.get("id") or item.get("voice_id"),
                            "title": item.get("title") or item.get("name"),
                            "status": item.get("status", "unknown"),
                        }
                        for item in items
                    ]
                return []
        except Exception as e:
            logger.error(f"获取 Fish Audio 语音列表失败: {e}")
            return []

    # ═══════════════════════════════════════════════════════════════════════
    # Edge TTS（兜底引擎，始终可用）
    # ═══════════════════════════════════════════════════════════════════════

    async def tts_edge_async(
        self,
        text: str,
        voice: str = "zh-CN-YunxiNeural",
        rate: str = "+0%",
    ) -> bytes:
        """
        Edge TTS 生成音频（异步版本）。
        
        Args:
            text: 要合成的文本
            voice: 语音名称
            rate: 语速，如 "+10%", "-20%", "+0%"
            
        Returns:
            bytes: mp3 音频数据
        """
        import edge_tts

        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
        audio_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])

        if not audio_chunks:
            raise RuntimeError("Edge TTS 未生成任何音频数据")

        return b"".join(audio_chunks)

    def tts_edge(
        self,
        text: str,
        voice: str = "zh-CN-YunxiNeural",
    ) -> bytes:
        """
        Edge TTS 生成音频（同步封装）。
        
        Args:
            text: 要合成的文本
            voice: 语音名称（默认：男播音员 云希）
            
        Returns:
            bytes: mp3 音频数据
        """
        import edge_tts

        # 在同步上下文中运行 async
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果已有运行中的事件循环，创建新任务
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, self.tts_edge_async(text, voice))
                    return future.result(timeout=120)
            else:
                return loop.run_until_complete(self.tts_edge_async(text, voice))
        except RuntimeError:
            return asyncio.run(self.tts_edge_async(text, voice))

    # ═══════════════════════════════════════════════════════════════════════
    # 语音列表
    # ═══════════════════════════════════════════════════════════════════════

    def get_preset_voices(self) -> list[dict]:
        """返回预设语音列表（Edge TTS）"""
        return [
            {"id": "edge_yunxi",    "name": "男播音员", "emoji": "🧑", "style": "沉稳大气", "source": "edge", "voice_key": "zh-CN-YunxiNeural"},
            {"id": "edge_xiaoxiao", "name": "女播音员", "emoji": "👩", "style": "温柔清晰", "source": "edge", "voice_key": "zh-CN-XiaoxiaoNeural"},
            {"id": "edge_yunjian",  "name": "电影解说", "emoji": "🎬", "style": "戏谑幽默", "source": "edge", "voice_key": "zh-CN-YunjianNeural"},
            {"id": "edge_yunyang",  "name": "技术极客", "emoji": "🤖", "style": "快速理性", "source": "edge", "voice_key": "zh-CN-YunyangNeural"},
        ]

    def get_all_voices(self, cloned_voices: list[dict] = None) -> list[dict]:
        """获取所有可用语音（预设 + 已克隆）"""
        voices = self.get_preset_voices()

        # 标记预设语音为始终就绪
        for v in voices:
            v["is_ready"] = True
            v["voice_id"] = v["id"]

        # 添加已克隆的语音
        if cloned_voices:
            for cv in cloned_voices:
                voices.append({
                    "id": cv.get("id", ""),
                    "name": cv.get("name", ""),
                    "emoji": "🎙️",
                    "style": "克隆语音",
                    "source": "fish",
                    "is_ready": cv.get("status") == "ready",
                    "voice_id": cv.get("fish_voice_id") or cv.get("id"),
                    "status": cv.get("status", "unknown"),
                })

        return voices

    def is_demo_mode(self) -> bool:
        """是否处于演示模式（无 Fish Audio API Key）"""
        return self._demon_mode

    # ═══════════════════════════════════════════════════════════════════════
    # 演示模式
    # ═══════════════════════════════════════════════════════════════════════

    def _demo_clone(self, name: str) -> dict:
        """演示模式：模拟克隆，返回一个预设语音"""
        demo_id = f"demo_{uuid.uuid4().hex[:8]}"
        logger.info(f"🎭 演示模式：模拟克隆语音「{name}」→ {demo_id}")
        return {
            "voice_id": demo_id,
            "name": name,
            "status": "ready",
            "preview_url": "",
            "demo_mode": True,
            "message": "演示模式：无 Fish Audio API Key，语音克隆为模拟。请设置 FISH_AUDIO_API_KEY 环境变量启用真实克隆。",
        }


# ── 全局单例 ──────────────────────────────────────────────────────────────
voice_service = VoiceService()


# ── 测试入口 ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    async def test():
        vs = VoiceService()
        print(f"Fish API Key: {'已设置' if vs.fish_api_key else '❌ 未设置（演示模式）'}")

        # 测试 Edge TTS
        text = "今天AI圈最重要的三件事：第一，OpenAI宣布完成新一轮融资"
        print(f"\n测试 Edge TTS: {text[:40]}...")
        audio = vs.tts_edge(text, "zh-CN-YunxiNeural")
        output_path = "/tmp/test_tts_edge.mp3"
        with open(output_path, "wb") as f:
            f.write(audio)
        print(f"✅ Edge TTS 测试通过 → {output_path} ({len(audio)} bytes)")

        # 显示预设语音
        print("\n预设语音:")
        for v in vs.get_preset_voices():
            print(f"  {v['emoji']} {v['name']} ({v['style']}) → {v['voice_key']}")

    asyncio.run(test())
