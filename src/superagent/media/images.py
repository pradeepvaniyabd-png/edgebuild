"""Pillow helpers: exact-size fitting, data URIs and simple technical image metrics."""

from __future__ import annotations

import base64
import io
import mimetypes
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

from ..errors import MediaError


def fit_image(path: Path, width: int, height: int, dest: Path | None = None) -> Path:
    """Cover-crop to exactly ``width``x``height`` so every provider's output matches the brief."""
    dest = dest or path
    try:
        with Image.open(path) as opened:
            image: Image.Image = ImageOps.exif_transpose(opened)
            if image.size != (width, height):
                image = ImageOps.fit(image.convert("RGB"), (width, height), Image.Resampling.LANCZOS)
            elif image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")
            dest.parent.mkdir(parents=True, exist_ok=True)
            image.save(dest)
    except (OSError, ValueError) as exc:
        raise MediaError(f"could not process image {path.name}: {exc}") from exc
    return dest


def to_data_uri(path: Path, *, max_bytes: int = 900_000, max_side: int = 1536) -> str:
    """Encode an image as a data URI, downscaling to JPEG when it exceeds ``max_bytes``."""
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if len(raw) > max_bytes:
        with Image.open(path) as opened:
            image = opened.convert("RGB")
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            for quality in (90, 80, 70, 60):
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=quality)
                raw = buffer.getvalue()
                if len(raw) <= max_bytes:
                    break
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def image_b64(path: Path) -> tuple[str, str]:
    """(media_type, base64) for vision requests."""
    uri = to_data_uri(path, max_bytes=3_500_000, max_side=2048)
    header, data = uri.split(",", 1)
    return header[5:].split(";")[0], data


def technical_metrics(path: Path) -> dict[str, float]:
    """Cheap, deterministic signals: sharpness, contrast, brightness, dominant-colour share.

    These catch blank, flat or badly blurred outputs. They say nothing about whether
    the picture matches the prompt; that needs a vision model.
    """
    with Image.open(path) as image:
        gray = image.convert("L")
        gray.thumbnail((512, 512))
        stat = ImageStat.Stat(gray)
        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_stat = ImageStat.Stat(edges)
        histogram = gray.histogram()
        total = max(1, sum(histogram))
        return {
            "brightness": stat.mean[0] / 255.0,
            "contrast": stat.stddev[0] / 127.5,
            "sharpness": edge_stat.mean[0] / 255.0,
            "dominant_share": max(histogram) / total,
        }


def color_signature(path: Path, bins: int = 4) -> list[float]:
    """Normalised coarse RGB histogram, used to compare visual consistency between frames."""
    with Image.open(path) as image:
        small = image.convert("RGB").resize((64, 64))
        counts = [0] * (bins**3)
        step = 256 // bins
        raw = small.tobytes()
        for i in range(0, len(raw), 3):
            counts[(raw[i] // step) * bins * bins + (raw[i + 1] // step) * bins + (raw[i + 2] // step)] += 1
        total = float(sum(counts)) or 1.0
        return [count / total for count in counts]


def histogram_similarity(a: list[float], b: list[float]) -> float:
    """Histogram intersection in [0, 1]; 1 means identical colour distribution."""
    return float(sum(min(x, y) for x, y in zip(a, b, strict=True)))
