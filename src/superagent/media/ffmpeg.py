"""FFmpeg invocation and shared encode settings."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..errors import MediaError
from ..proc import ProcessResult, run_process

VIDEO_ENCODE = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
AUDIO_ENCODE = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
FASTSTART = ["-movflags", "+faststart"]


async def ffmpeg(args: Sequence[str | Path], *, timeout: float = 900.0) -> ProcessResult:
    """Run ffmpeg with safe defaults. Raises ``MediaError`` carrying the tail of stderr on failure."""
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", *[str(a) for a in args]]
    result = await run_process(argv, timeout=timeout)
    if not result.ok:
        raise MediaError(f"ffmpeg failed ({result.returncode}): {result.tail(700)}")
    return result


def escape_filter_path(path: Path) -> str:
    """Escape a path for use inside a filtergraph option value (subtitles=, movie= ...)."""
    text = str(path).replace("\\", "/")
    for char in (":", "'", ",", "[", "]", ";"):
        text = text.replace(char, f"\\{char}")
    return text
