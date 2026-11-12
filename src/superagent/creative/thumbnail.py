"""Thumbnails and cover images: cover-fit a frame, add a legible title, measure legibility."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from ..errors import MediaError
from ..media.fonts import find_font
from ..media.images import technical_metrics

SIZES = {"youtube": (1280, 720), "shorts": (1080, 1920), "instagram": (1080, 1350), "square": (1080, 1080)}


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and draw.textlength(trial, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def create(source: Path, title: str, out: Path, *, size: tuple[int, int] = (1280, 720), accent: tuple[int, int, int] = (255, 214, 10),
           max_lines: int = 3) -> dict[str, Any]:  # fmt: skip
    """Render ``title`` over ``source``. Returns the measurements used to judge mobile legibility."""
    width, height = size
    try:
        with Image.open(source) as image:
            base = ImageOps.fit(ImageOps.exif_transpose(image).convert("RGB"), size, Image.Resampling.LANCZOS)
    except OSError as exc:
        raise MediaError(f"cannot open the thumbnail source {source.name}: {exc}") from exc
    sharpness = technical_metrics(source)["sharpness"]

    # Darken the lower part so white text always has contrast, whatever the picture is.
    gradient = Image.new("L", (1, height))
    for y in range(height):
        gradient.putpixel((0, y), int(215 * max(0.0, (y / height - 0.38) / 0.62) ** 1.2))
    shade = Image.new("RGB", size, (8, 8, 12))
    base = Image.composite(shade, base, gradient.resize(size))

    text = " ".join(title.split())
    draw = ImageDraw.Draw(base)
    font_path = find_font(text, bold=True)
    lines: list[str] = []
    font_size = int(height * 0.17)
    font = None
    while font_size >= int(height * 0.07):
        font = ImageFont.truetype(str(font_path), font_size, layout_engine=ImageFont.Layout.RAQM) if font_path else ImageFont.load_default(font_size)
        lines = _wrap(draw, text, font, int(width * 0.88))
        if len(lines) <= max_lines:
            break
        font_size = int(font_size * 0.92)
    lines = lines[:max_lines]
    assert font is not None
    line_height = int(font_size * 1.28)
    block = line_height * len(lines)
    y = height - block - int(height * 0.06)
    stroke = max(3, font_size // 14)
    for index, line in enumerate(lines):
        line_width = draw.textlength(line, font=font)
        draw.text(((width - line_width) / 2, y + index * line_height), line, font=font, fill=accent if index == 0 else (255, 255, 255),
                  stroke_width=stroke, stroke_fill=(0, 0, 0))  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    base.save(out, quality=94) if out.suffix.lower() in {".jpg", ".jpeg"} else base.save(out)

    region = base.crop((0, y, width, y + block)).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    ratio = contrast_ratio((255, 255, 255), region)  # type: ignore[arg-type]
    text_height_ratio = font_size / height
    return {
        "path": str(out), "size": list(size), "lines": lines, "font": font_path.name if font_path else "default",
        "font_px": font_size, "text_height_ratio": round(text_height_ratio, 3), "contrast_ratio": round(ratio, 2),
        "source_sharpness": round(sharpness, 4),
        "mobile_readable": text_height_ratio >= 0.065 and ratio >= 4.5 and len(lines) <= max_lines,
        "truncated": len(_wrap(draw, text, font, int(width * 0.88))) > max_lines,
    }  # fmt: skip
