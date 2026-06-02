#!/usr/bin/env python3
"""封面图生成器 v7：全名大字 + 品牌色背景 — 缩略图可辨"""

import os
from PIL import Image, ImageDraw, ImageFont

WORKDIR = os.path.dirname(os.path.abspath(__file__))
COVER_PATH = os.path.join(WORKDIR, "cover.png")

BRAND_COLORS = {
    "OpenAI":      "#10A37F",
    "Anthropic":   "#D97757",
    "Claude":      "#D97757",
    "Google":      "#4285F4",
    "DeepMind":    "#0F9D58",
    "Gemini":      "#4285F4",
    "Meta":        "#1877F2",
    "NVIDIA":      "#76B900",
    "Microsoft":   "#0078D4",
    "Apple":       "#555555",
    "Amazon":      "#FF9900",
    "HuggingFace": "#FFD21E",
    "Tesla":       "#E82127",
    "YC":          "#FF6600",
    "xAI":         "#E82127",
    "Stability":   "#7B2FF7",
    "Mistral":     "#F59E0B",
    "Cohere":      "#1BBD72",
    "Midjourney":  "#6366F1",
    "Runway":      "#000000",
    "Perplexity":  "#1DB954",
    "Notion":      "#000000",
    "Cursor":      "#6366F1",
    "Cognition":   "#8B5CF6",
    "Sakana":      "#EC4899",
    "Databricks":  "#FF3621",
    "Scale":       "#2563EB",
    "Glean":       "#059669",
    "Harvey":      "#DC2626",
    "Replit":      "#F97316",
    "Vercel":      "#000000",
    "ElevenLabs":  "#7C3AED",
    "Suno":        "#F59E0B",
    "Udio":        "#EC4899",
    "Fal":         "#8B5CF6",
    "Together":    "#2563EB",
    "Groq":        "#F97316",
    "Cerebras":    "#DC2626",
    "SambaNova":   "#059669",

    "Sam Altman":       "#10A37F",
    "Ilya Sutskever":   "#10A37F",
    "Dario Amodei":     "#D97757",
    "Jensen Huang":     "#76B900",
    "Elon Musk":        "#E82127",
    "Demis Hassabis":   "#0F9D58",
    "Yann LeCun":       "#1877F2",
    "Geoffrey Hinton":  "#555555",
    "Fei-Fei Li":       "#6366F1",
    "Andrej Karpathy":  "#E82127",
    "Lex Fridman":      "#F59E0B",
    "Sundar Pichai":    "#4285F4",
    "Satya Nadella":    "#0078D4",
    "Tim Cook":         "#555555",
    "Mark Zuckerberg":  "#1877F2",
    "Jeff Dean":        "#4285F4",
    "Noam Shazeer":     "#4285F4",
    "Jim Fan":          "#76B900",
    "Yoshua Bengio":    "#555555",
    "Andrew Ng":        "#DC2626",
}

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
]


def _load_font(size: int):
    for fp in FONT_CANDIDATES:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default()


def generate_cover(headline_title: str, date_str: str = "", output_path: str = None) -> str:
    output_path = output_path or COVER_PATH
    title_lower = (headline_title or "").lower()

    # 匹配品牌/人物
    entity, bg_color = "AI", "#5C3317"
    for name, clr in BRAND_COLORS.items():
        if name.lower() in title_lower:
            entity, bg_color = name, clr
            break

    W, H = 900, 500

    # ── 纯色背景 ──
    img = Image.new("RGB", (W, H), bg_color)
    draw = ImageDraw.Draw(img)

    # ── 动态字号：名越短字越大 ──
    name_len = len(entity)
    if name_len <= 5:
        font_size = 130
    elif name_len <= 8:
        font_size = 100
    elif name_len <= 12:
        font_size = 75
    else:
        font_size = 55

    font = _load_font(font_size)

    # 如果一行放不下，折行
    max_width = W - 80
    bbox = draw.textbbox((0, 0), entity, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    lines = [entity]
    if tw > max_width and " " in entity:
        # 人名按空格折行
        parts = entity.split()
        mid = len(parts) // 2
        line1 = " ".join(parts[:mid])
        line2 = " ".join(parts[mid:])
        # 重新测量
        b1 = draw.textbbox((0, 0), line1, font=font)
        b2 = draw.textbbox((0, 0), line2, font=font)
        if max(b1[2] - b1[0], b2[2] - b2[0]) <= max_width:
            lines = [line1, line2]
            th = (b1[3] - b1[1]) * 2 + 10

    # 居中绘制
    total_h = th if len(lines) == 1 else sum(
        draw.textbbox((0, 0), l, font=font)[3] - draw.textbbox((0, 0), l, font=font)[1]
        for l in lines
    ) + (len(lines) - 1) * 10

    start_y = (H - total_h) // 2 - 10
    for i, line in enumerate(lines):
        bb = draw.textbbox((0, 0), line, font=font)
        lw, lh = bb[2] - bb[0], bb[3] - bb[1]
        lx = (W - lw) // 2
        draw.text((lx, start_y), line, fill="white", font=font)
        start_y += lh + 10

    # ── 底部小标签 ──
    tag = "硅谷AI晨报"
    font_tag = _load_font(20)
    tbbox = draw.textbbox((0, 0), tag, font=font_tag)
    sw = tbbox[2] - tbbox[0]
    draw.text(((W - sw) // 2, H - 36), tag, fill=(255, 255, 255, 100), font=font_tag)

    img.save(output_path, "PNG")
    return output_path


if __name__ == "__main__":
    tests = [
        "Sam Altman宣布OpenAI成立基金会",
        "Anthropic发布Claude Opus 4.8",
        "Jensen Huang谈NVIDIA下一代GPU",
        "Google DeepMind发布Co-Scientist",
        "Elon Musk的xAI发布新模型",
        "Dario Amodei最新访谈",
    ]
    for t in tests:
        p = generate_cover(t)
        size = os.path.getsize(p)
        # 找匹配名
        name = next((n for n in BRAND_COLORS if n.lower() in t.lower()), "AI")
        white_pct = __import__('numpy').array(Image.open(p))[:,:,0]
        white_pct = ((white_pct > 200)).mean() * 100
        print(f"  {name:20s} {t[:35]:35s} {size:>6d}B 白色{white_pct:5.1f}%")
