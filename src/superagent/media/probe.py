"""ffprobe / Pillow inspection. The single source of truth for "is this media file valid"."""

from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..errors import FileProblem, MediaError
from ..proc import run_process


class VideoStream(BaseModel):
    codec: str
    width: int
    height: int
    fps: float
    pix_fmt: str | None = None
    frames: int | None = None


class AudioStream(BaseModel):
    codec: str
    sample_rate: int | None = None
    channels: int | None = None


class MediaInfo(BaseModel):
    path: str
    container: str
    duration: float
    size_bytes: int
    bit_rate: int | None = None
    video: VideoStream | None = None
    audio: AudioStream | None = None

    @property
    def has_video(self) -> bool:
        return self.video is not None

    @property
    def has_audio(self) -> bool:
        return self.audio is not None

    @property
    def aspect_ratio(self) -> float | None:
        if self.video is None or not self.video.height:
            return None
        return self.video.width / self.video.height


class ImageInfo(BaseModel):
    path: str
    format: str
    width: int
    height: int
    mode: str
    size_bytes: int

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height else 0.0


def _rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _num(value: Any, cast: type = float) -> Any:
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


async def probe(path: str | os.PathLike[str]) -> MediaInfo:
    """Inspect an audio/video file. Raises ``MediaError`` when ffprobe cannot read it."""
    file = Path(path)
    if not file.is_file():
        raise FileProblem(f"media file not found: {file}")
    result = await run_process(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(file)],
        timeout=60,
    )
    if not result.ok:
        raise MediaError(f"ffprobe could not read {file.name}: {result.tail(400)}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned unreadable output for {file.name}") from exc
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    if not streams:
        raise MediaError(f"{file.name} contains no media streams")

    video = audio = None
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video" and video is None and stream.get("disposition", {}).get("attached_pic") != 1:
            video = VideoStream(
                codec=stream.get("codec_name", "unknown"),
                width=int(stream.get("width") or 0),
                height=int(stream.get("height") or 0),
                fps=_rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate")),
                pix_fmt=stream.get("pix_fmt"),
                frames=_num(stream.get("nb_frames"), int),
            )
        elif kind == "audio" and audio is None:
            audio = AudioStream(
                codec=stream.get("codec_name", "unknown"),
                sample_rate=_num(stream.get("sample_rate"), int),
                channels=_num(stream.get("channels"), int),
            )
    duration = _num(fmt.get("duration")) or max((_num(s.get("duration")) or 0.0 for s in streams), default=0.0)
    return MediaInfo(
        path=str(file),
        container=str(fmt.get("format_name", "unknown")),
        duration=float(duration or 0.0),
        size_bytes=int(_num(fmt.get("size"), int) or file.stat().st_size),
        bit_rate=_num(fmt.get("bit_rate"), int),
        video=video,
        audio=audio,
    )


def probe_image(path: str | os.PathLike[str]) -> ImageInfo:
    """Inspect an image and verify it decodes fully."""
    from PIL import Image, UnidentifiedImageError

    file = Path(path)
    if not file.is_file():
        raise FileProblem(f"image file not found: {file}")
    try:
        with Image.open(file) as image:
            image.verify()
        with Image.open(file) as image:
            image.load()
            return ImageInfo(
                path=str(file), format=image.format or "unknown", width=image.width, height=image.height,
                mode=image.mode, size_bytes=file.stat().st_size,
            )  # fmt: skip
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise MediaError(f"{file.name} is not a valid image: {exc}") from exc
