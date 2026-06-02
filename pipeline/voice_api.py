"""
语音管理 API Blueprint
路由挂载到 Flask 应用

端点：
  POST   /api/v2/voice/clone       - 上传音频，克隆语音
  GET    /api/v2/voice/list        - 获取语音列表
  POST   /api/v2/voice/tts         - 生成 TTS 音频
  DELETE /api/v2/voice/<voice_id>  - 删除克隆语音
"""

import os
import uuid
import logging
from functools import wraps

from flask import Blueprint, request, jsonify, Response, current_app

from voice_service import voice_service, EDGE_VOICE_MAP
from voice_db import (
    init_voice_db,
    insert_cloned_voice,
    get_cloned_voices,
    get_cloned_voice,
    update_voice_status,
    delete_cloned_voice,
)

logger = logging.getLogger(__name__)

# ── Blueprint ──────────────────────────────────────────────────────────────
voice_bp = Blueprint("voice", __name__, url_prefix="/api/v2/voice")

# ── 上传目录 ───────────────────────────────────────────────────────────────
WORKDIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(WORKDIR, "voice_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── 认证 ───────────────────────────────────────────────────────────────────
# 从环境变量或 main app config 获取 token
API_TOKEN = os.environ.get("API_TOKEN", "hermes-morning-brief-2026")


def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ── API 端点 ───────────────────────────────────────────────────────────────


@voice_bp.route("/clone", methods=["POST"])
@require_token
async def voice_clone():
    """
    上传音频文件，克隆语音。

    multipart/form-data:
        audio (file): 音频文件（mp3/wav/m4a/mp4/mov，最大 10MB，时长 5s~3min）
        name  (str) : 语音名称
        description (str, optional): 语音描述

    返回:
        {voice_id, name, status: "training"/"ready", preview_url}
    """
    try:
        if "audio" not in request.files:
            return jsonify({"error": "缺少 audio 文件"}), 400

        audio_file = request.files["audio"]
        name = request.form.get("name", "").strip()

        if not name:
            return jsonify({"error": "缺少 name 参数"}), 400

        if audio_file.filename == "":
            return jsonify({"error": "文件名为空"}), 400

        # 检查文件大小
        audio_file.seek(0, os.SEEK_END)
        file_size = audio_file.tell()
        audio_file.seek(0)

        if file_size > 10 * 1024 * 1024:
            return jsonify({"error": f"文件过大（{file_size} bytes），最大支持 10MB"}), 400

        # 检查文件扩展名
        ext = os.path.splitext(audio_file.filename)[1].lower().lstrip(".")
        allowed_exts = {"mp3", "wav", "m4a", "mp4", "mov", "flac", "ogg", "aac", "wma"}
        if ext not in allowed_exts:
            return jsonify({
                "error": f"不支持的文件格式: .{ext}，支持: {', '.join(sorted(allowed_exts))}",
            }), 400

        # 保存到本地
        local_id = uuid.uuid4().hex[:12]
        local_filename = f"{local_id}.{ext}"
        local_path = os.path.join(UPLOAD_DIR, local_filename)
        audio_file.save(local_path)

        logger.info(f"音频已保存: {local_path} ({file_size} bytes)")

        # 调用 Fish Audio 克隆
        description = request.form.get("description", "")
        result = await voice_service.clone_voice(local_path, name, description)

        if "error" in result:
            return jsonify(result), 500

        # 写入数据库
        insert_cloned_voice(
            voice_id=local_id,
            name=name,
            fish_voice_id=result.get("voice_id", ""),
            source_audio_path=local_path,
            status=result.get("status", "training"),
            preview_url=result.get("preview_url", ""),
        )

        # 构建响应
        response_data = {
            "id": local_id,
            "name": name,
            "status": result.get("status", "training"),
            "preview_url": result.get("preview_url", ""),
            "fish_voice_id": result.get("voice_id", ""),
        }

        if result.get("demo_mode"):
            response_data["demo_mode"] = True
            response_data["message"] = result.get("message", "")

        return jsonify(response_data)

    except Exception as e:
        logger.error(f"/api/v2/voice/clone 错误: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@voice_bp.route("/list", methods=["GET"])
def voice_list():
    """
    获取所有可用语音列表（预设 + 已克隆）。
    
    返回:
        [{id, name, emoji, style, source, is_ready, voice_key?}]
    """
    try:
        cloned = get_cloned_voices()
        voices = voice_service.get_all_voices(cloned)

        return jsonify({
            "voices": voices,
            "total": len(voices),
            "demo_mode": voice_service.is_demo_mode(),
        })

    except Exception as e:
        logger.error(f"/api/v2/voice/list 错误: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@voice_bp.route("/tts", methods=["POST"])
@require_token
def voice_tts():
    """
    生成 TTS 音频。

    JSON body:
        text     (str)  : 要合成的文本
        voice_id (str)  : 语音 ID（预设 edge_xxx 或克隆的 id）
        speed    (float): 语速，默认 1.0（0.5 ~ 2.0）

    返回:
        audio/mpeg 音频流
    """
    try:
        data = request.get_json(force=True)
        text = data.get("text", "").strip()
        voice_id = data.get("voice_id", "").strip()
        speed = float(data.get("speed", 1.0))

        if not text:
            return jsonify({"error": "text 不能为空"}), 400

        if not voice_id:
            return jsonify({"error": "voice_id 不能为空"}), 400

        # 判断是预设 Edge 语音还是克隆语音
        if voice_id in EDGE_VOICE_MAP:
            # Edge TTS 预设语音
            voice_key = EDGE_VOICE_MAP[voice_id]["voice"]
            logger.info(f"使用 Edge TTS: {voice_id} → {voice_key}")

            try:
                audio_bytes = voice_service.tts_edge(text, voice_key)
            except Exception as e:
                logger.error(f"Edge TTS 失败: {e}")
                return jsonify({"error": f"TTS 生成失败: {e}"}), 500

            return Response(
                audio_bytes,
                mimetype="audio/mpeg",
                headers={
                    "Content-Disposition": "inline; filename=tts.mp3",
                    "X-Voice-Source": "edge",
                    "X-Voice-Id": voice_id,
                },
            )

        else:
            # 克隆语音，尝试 Fish Audio
            cloned = get_cloned_voice(voice_id)
            if not cloned:
                return jsonify({"error": f"语音 {voice_id} 不存在"}), 404

            if cloned.get("status") != "ready":
                return jsonify({
                    "error": f"语音 {voice_id} 尚未就绪，当前状态: {cloned.get('status')}",
                }), 400

            fish_voice_id = cloned.get("fish_voice_id", "")
            if not fish_voice_id:
                return jsonify({"error": f"语音 {voice_id} 缺少 Fish Audio voice_id"}), 500

            # 异步调用 Fish Audio TTS
            import asyncio

            async def _tts():
                return await voice_service.tts_generate(text, fish_voice_id, speed)

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, _tts())
                        audio_bytes = future.result(timeout=120)
                else:
                    audio_bytes = loop.run_until_complete(_tts())
            except RuntimeError:
                audio_bytes = asyncio.run(_tts())

            if audio_bytes is None:
                return jsonify({"error": "Fish Audio TTS 生成失败"}), 500

            return Response(
                audio_bytes,
                mimetype="audio/mpeg",
                headers={
                    "Content-Disposition": "inline; filename=tts.mp3",
                    "X-Voice-Source": "fish",
                    "X-Voice-Id": voice_id,
                },
            )

    except Exception as e:
        logger.error(f"/api/v2/voice/tts 错误: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@voice_bp.route("/<voice_id>", methods=["DELETE"])
@require_token
def voice_delete(voice_id: str):
    """
    删除克隆语音。

    路径参数:
        voice_id: 克隆语音 ID

    返回:
        {status: "ok"/"not_found"}
    """
    try:
        # 检查是否预设语音
        if voice_id in EDGE_VOICE_MAP:
            return jsonify({"error": "不能删除预设语音"}), 400

        cloned = get_cloned_voice(voice_id)
        if not cloned:
            return jsonify({"error": f"语音 {voice_id} 不存在"}), 404

        # 删除本地文件
        source_path = cloned.get("source_audio_path", "")
        if source_path and os.path.isfile(source_path):
            try:
                os.remove(source_path)
                logger.info(f"已删除音频文件: {source_path}")
            except OSError as e:
                logger.warning(f"删除文件失败: {e}")

        # 删除 Fish Audio 上的语音（异步）
        fish_voice_id = cloned.get("fish_voice_id", "")
        if fish_voice_id and not voice_service.is_demo_mode():
            import asyncio

            async def _delete():
                return await voice_service.delete_voice(fish_voice_id)

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, _delete())
                        future.result(timeout=30)
                else:
                    loop.run_until_complete(_delete())
            except Exception:
                pass  # 不阻塞删除本地记录

        # 删除数据库记录
        delete_cloned_voice(voice_id)

        return jsonify({"status": "ok", "deleted": voice_id})

    except Exception as e:
        logger.error(f"/api/v2/voice/{voice_id} DELETE 错误: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── 初始化（在 Blueprint 被注册时调用） ──────────────────────────────────
@voice_bp.record_once
def on_register(state):
    """Blueprint 注册时自动初始化语音数据库"""
    init_voice_db()
    logger.info("🎤 语音 API Blueprint 已注册，数据库已初始化")
