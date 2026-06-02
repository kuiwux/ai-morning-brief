#!/usr/bin/env python3
"""
微信公众号 API 封装
- get_access_token(): 获取 stable_token（互不踢）
- create_draft(): 创建图文草稿
- upload_cover(): 上传封面素材（简单方案）
"""

import os
import re
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import requests


def _minify_html(html: str) -> str:
    """压缩 HTML：去注释、合并空白、去标签间空格。"""
    # 1. 去掉 HTML 注释
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    # 2. 将连续空白（空格、换行、制表）合并为单个空格
    html = re.sub(r"\s+", " ", html)
    # 3. 去掉标签间的空格（> < → ><）
    html = re.sub(r">\s+<", "><", html)
    return html.strip()

# ============================================================
# 配置
# ============================================================

WORKDIR = os.path.dirname(os.path.abspath(__file__))
PROXY_URL = "http://172.23.80.1:7890"
HTTP_TIMEOUT = 30

logger = logging.getLogger("wechat_api")

# 全局 Session（走代理）
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
        _session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; AiMorningBrief/2.0)",
            "Content-Type": "application/json; charset=utf-8",
        })
    return _session


def _load_credentials() -> Tuple[str, str]:
    """从 .env 文件加载微信公众号 AppID 和 AppSecret"""
    appid = os.environ.get("WEIXIN_APPID", "")
    appsecret = os.environ.get("WEIXIN_APPSECRET", "")

    if not appid or not appsecret:
        env_file = os.path.join(WORKDIR, ".env")
        if os.path.exists(env_file):
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("WEIXIN_APPID="):
                        appid = appid or line.split("=", 1)[1]
                    elif line.startswith("WEIXIN_APPSECRET="):
                        appsecret = appsecret or line.split("=", 1)[1]

    if not appid or not appsecret:
        raise RuntimeError(
            "微信公众号密钥未配置，请在 .env 中设置 WEIXIN_APPID 和 WEIXIN_APPSECRET"
        )

    return appid, appsecret


# ============================================================
# Token 管理（内存缓存）
# ============================================================

_token_cache: Optional[str] = None
_token_expires_at: float = 0  # Unix timestamp


def get_access_token() -> str:
    """
    获取微信公众号 access_token（使用 stable_token 接口，互不踢）。

    返回 access_token 字符串。
    内置内存缓存，7200 秒内不重复请求。
    """
    global _token_cache, _token_expires_at

    # 检查缓存是否有效（提前 120 秒刷新）
    now = datetime.now(timezone.utc).timestamp()
    if _token_cache and now < (_token_expires_at - 120):
        return _token_cache

    appid, appsecret = _load_credentials()
    session = _get_session()

    url = "https://api.weixin.qq.com/cgi-bin/stable_token"
    body = {
        "grant_type": "client_credential",
        "appid": appid,
        "secret": appsecret,
    }

    try:
        resp = session.post(url, json=body, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if "errcode" in data and data["errcode"] != 0:
            raise RuntimeError(
                f"微信 access_token 获取失败: errcode={data.get('errcode')}, "
                f"errmsg={data.get('errmsg', 'unknown')}"
            )

        access_token = data.get("access_token", "")
        expires_in = data.get("expires_in", 7200)

        if not access_token:
            raise RuntimeError("微信返回的 access_token 为空")

        # 更新缓存
        _token_cache = access_token
        _token_expires_at = now + expires_in

        logger.info(f"✅ 微信 access_token 已获取（有效期 {expires_in} 秒）")
        return access_token

    except requests.RequestException as e:
        raise RuntimeError(f"微信 API 网络请求失败: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"微信 API 返回数据解析失败: {e}")


# ============================================================
# 封面图上传
# ============================================================

def upload_cover(access_token: str, image_path: Optional[str] = None) -> Optional[str]:
    """
    上传封面图素材到微信永久素材库。

    Args:
        access_token: 公众号 access_token
        image_path: 本地图片路径（jpg/png，2M 以内）。
                    如果为 None，尝试生成一张纯色占位图。

    Returns:
        素材 media_id，失败返回 None
    """
    if image_path and not os.path.exists(image_path):
        logger.warning(f"⚠️ 封面图片不存在: {image_path}")
        image_path = None

    if image_path is None:
        # 简单方案：生成一张 900x500 的纯色 PNG 占位图
        try:
            image_path = _generate_placeholder_cover()
        except Exception as e:
            logger.warning(f"⚠️ 无法生成占位封面图: {e}")
            return None

    session = _get_session()
    url = "https://api.weixin.qq.com/cgi-bin/material/add_material"
    params = {"access_token": access_token, "type": "image"}

    try:
        with open(image_path, "rb") as f:
            # 上传时 Content-Type 需要是 multipart/form-data，requests 自动处理
            files = {"media": (os.path.basename(image_path), f, "image/png")}
            # 清除 json content-type header
            resp = requests.post(
                url,
                params=params,
                files=files,
                timeout=HTTP_TIMEOUT,
                proxies={"http": PROXY_URL, "https": PROXY_URL},
            )
            resp.raise_for_status()
            data = resp.json()

            if "errcode" in data and data["errcode"] != 0:
                logger.warning(
                    f"⚠️ 封面上传失败: errcode={data.get('errcode')}, "
                    f"errmsg={data.get('errmsg', 'unknown')}"
                )
                return None

            media_id = data.get("media_id", "")
            if media_id:
                logger.info(f"✅ 封面图已上传: media_id={media_id}")
                return media_id
            else:
                logger.warning("⚠️ 封面上传返回空 media_id")
                return None

    except requests.RequestException as e:
        logger.warning(f"⚠️ 封面上传请求失败: {e}")
        return None
    except Exception as e:
        logger.warning(f"⚠️ 封面上传异常: {e}")
        return None


def _generate_placeholder_cover() -> str:
    """生成一张纯色占位封面图（900x500 PNG），返回文件路径"""
    # 尝试用 Pillow，如果没有则用纯 bytes 构造最小 PNG
    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.new("RGB", (900, 500), color=(139, 69, 19))  # 棕色
        draw = ImageDraw.Draw(img)

        # 绘制文字
        beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
        date_str = beijing_now.strftime("%Y年%m月%d日")
        text_lines = ["硅谷AI晨报", date_str]

        # 尝试使用中文字体
        font_paths = [
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        font = None
        for fp in font_paths:
            if os.path.exists(fp):
                try:
                    font = ImageFont.truetype(fp, 60)
                    break
                except Exception:
                    continue

        y = 150
        for line in text_lines:
            if font:
                bbox = draw.textbbox((0, 0), line, font=font)
                w = bbox[2] - bbox[0]
                x = (900 - w) // 2
                draw.text((x, y), line, fill=(255, 255, 255), font=font)
            else:
                draw.text((450, y), line, fill=(255, 255, 255), anchor="mm")
            y += 80

        path = os.path.join(WORKDIR, "cover_placeholder.png")
        img.save(path, "PNG")
        return path

    except ImportError:
        # 无 Pillow，生成最小 PNG（纯色）
        logger.info("Pillow 未安装，生成纯色 PNG 占位图")
        return _generate_minimal_png()


def _generate_minimal_png() -> str:
    """生成一张 900x500 棕色纯色最小 PNG"""
    import struct
    import zlib

    width, height = 900, 500
    # 棕色 RGB: #8B4513 -> (139, 69, 19)
    r, g, b = 139, 69, 19

    # PNG 签名
    signature = b"\x89PNG\r\n\x1a\n"

    # IHDR chunk
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)

    # IDAT chunk（原始像素数据 + zlib 压缩）
    raw_data = b""
    for y in range(height):
        raw_data += b"\x00"  # filter none
        raw_data += bytes([r, g, b]) * width

    compressed = zlib.compress(raw_data)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat = struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", idat_crc)

    # IEND chunk
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)

    path = os.path.join(WORKDIR, "cover_placeholder.png")
    with open(path, "wb") as f:
        f.write(signature + ihdr + idat + iend)

    return path


# ============================================================
# 草稿创建
# ============================================================

def create_draft(
    access_token: str,
    html_content: str,
    title: Optional[str] = None,
    thumb_media_id: Optional[str] = None,
    digest: Optional[str] = None,
    content_source_url: str = "",
) -> Optional[str]:
    """
    创建微信公众号图文草稿。

    Args:
        access_token: 公众号 access_token
        html_content: 图文正文 HTML
        title: 标题，默认自动生成（含日期）
        thumb_media_id: 封面图素材 ID，可为空
        digest: 摘要（54 字以内），默认自动截取
        content_source_url: 原文链接

    Returns:
        草稿 media_id，失败返回 None
    """
    if not access_token:
        logger.error("❌ access_token 为空，无法创建草稿")
        return None

    if not html_content:
        logger.error("❌ html_content 为空，无法创建草稿")
        return None

    # 自动生成标题
    if not title:
        beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
        date_str = beijing_now.strftime("%Y年%m月%d日")
        title = f"硅谷AI晨报 | {date_str}"

    # 自动截取摘要（54 字以内）
    if not digest:
        # 从 HTML 中提取纯文本作为摘要
        import re
        plain = re.sub(r"<[^>]+>", "", html_content)
        plain = plain.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        plain = " ".join(plain.split())  # 合并空白
        digest = plain[:54].strip()
        if len(plain) > 54:
            digest += "..."

    # 如果 digest 为空，使用默认摘要
    if not digest:
        digest = title

    url = f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={access_token}"

    # 压缩 HTML 以符合微信内容长度限制
    compact_html = _minify_html(html_content)
    logger.info(f"📦 HTML 压缩: {len(html_content)} → {len(compact_html)} 字节")

    articles = [{
        "title": title,
        "content": compact_html,
        "digest": digest,
    }]

    if thumb_media_id:
        articles[0]["thumb_media_id"] = thumb_media_id
    if content_source_url:
        articles[0]["content_source_url"] = content_source_url

    body = {"articles": articles}

    session = _get_session()
    try:
        # 关键：ensure_ascii=False 避免中文被转成 \uXXXX，微信草稿系统可能不解码
        json_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        resp = session.post(
            url,
            data=json_bytes,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        if "errcode" in data and data["errcode"] != 0:
            logger.error(
                f"❌ 微信草稿创建失败: errcode={data.get('errcode')}, "
                f"errmsg={data.get('errmsg', 'unknown')}"
            )
            return None

        media_id = data.get("media_id", "")
        if media_id:
            logger.info(f"✅ 微信草稿已创建: draft_id={media_id}")
            return media_id
        else:
            logger.warning(f"⚠️ 草稿创建返回空 media_id，原始响应: {data}")
            return None

    except requests.RequestException as e:
        logger.error(f"❌ 微信草稿创建网络请求失败: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"❌ 微信草稿创建返回数据解析失败: {e}")
        return None


# ============================================================
# 发布草稿
# ============================================================

def publish_draft(access_token: str, media_id: str) -> bool:
    """发布草稿（直接群发，无需人工审核）"""
    if not access_token or not media_id:
        return False

    url = f"https://api.weixin.qq.com/cgi-bin/freepublish/submit?access_token={access_token}"
    body = {"media_id": media_id}

    session = _get_session()
    try:
        json_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        resp = session.post(
            url,
            data=json_bytes,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        if "errcode" in data and data["errcode"] != 0:
            logger.error(f"❌ 发布失败: errcode={data.get('errcode')}, errmsg={data.get('errmsg')}")
            return False

        publish_id = data.get("publish_id", "")
        logger.info(f"✅ 草稿已发布: publish_id={publish_id}")
        return True

    except Exception as e:
        logger.error(f"❌ 发布请求失败: {e}")
        return False


# ============================================================
# 独立测试入口
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 1. 获取 token
    try:
        token = get_access_token()
        print(f"Token: {token[:20]}...")
    except Exception as e:
        print(f"❌ Token 获取失败: {e}")
        exit(1)

    # 2. 尝试上传封面（可选）
    cover_id = None
    try:
        cover_id = upload_cover(token)
    except Exception as e:
        print(f"⚠️ 封面上传跳过: {e}")

    # 3. 创建测试草稿
    test_html = "<h1>测试标题</h1><p>这是一条测试草稿。</p>"
    draft_id = create_draft(token, test_html, thumb_media_id=cover_id)
    if draft_id:
        print(f"✅ 测试草稿创建成功: {draft_id}")
    else:
        print("⚠️ 测试草稿创建失败（可能是权限不足或接口限制）")
