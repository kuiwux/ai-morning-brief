#!/usr/bin/env python3
"""Generate PWA icons for 硅谷AI晨报"""
from PIL import Image, ImageDraw, ImageFont
import os

def create_icon(size, filename, bg_color="#C4A06A", text="AI晨", font_size_ratio=0.38):
    """Create a rounded-square icon with text"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded square background
    radius = int(size * 0.22)
    draw.rounded_rectangle(
        [(0, 0), (size - 1, size - 1)],
        radius=radius,
        fill=bg_color
    )

    # Try to find a Chinese font
    font_paths = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    
    font = None
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, int(size * font_size_ratio))
                break
            except Exception:
                continue

    if font is None:
        # Fallback: try smaller sizes
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", int(size * font_size_ratio))
        except Exception:
            font = ImageFont.load_default()

    # Draw text centered
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (size - tw) / 2 - bbox[0]
    y = (size - th) / 2 - bbox[1]
    draw.text((x, y), text, fill="white", font=font)

    # For smaller icon, use newspaper emoji-style symbol
    img.save(filename, "PNG")
    print(f"Created {filename} ({size}x{size})")

# Generate all icons
out_dir = os.path.dirname(os.path.abspath(__file__))

create_icon(192, os.path.join(out_dir, "icon-192.png"), text="AI晨", font_size_ratio=0.35)
create_icon(512, os.path.join(out_dir, "icon-512.png"), text="AI晨", font_size_ratio=0.35)
create_icon(180, os.path.join(out_dir, "apple-touch-icon.png"), text="AI晨", font_size_ratio=0.35)
create_icon(32, os.path.join(out_dir, "favicon-32.png"), text="AI", font_size_ratio=0.38)
create_icon(1024, os.path.join(out_dir, "app-store-icon-1024.png"), text="AI晨报", font_size_ratio=0.35)

print("All icons generated successfully!")