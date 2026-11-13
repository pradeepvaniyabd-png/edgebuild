"""Subtitle cues from scene narration, and SRT / VTT / ASS writers.

Timing follows the real audio: a scene's cues are spread over its measured voice duration
in proportion to text length, then shifted to the scene's start in the final timeline.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..media.fonts import ass_font_name


@dataclass
class Cue:
    start: float
    end: float
    text: str  # lines joined with \n


def wrap_text(text: str, max_chars: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in re.sub(r"\s+", " ", text).strip().split(" "):
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def chunk_lines(lines: list[str], per_cue: int = 2) -> list[str]:
    return ["\n".join(lines[i : i + per_cue]) for i in range(0, len(lines), per_cue)]


def build_cues(scenes: list[tuple[float, float, str]], *, max_chars: int, lead_in: float = 0.12, tail_out: float = 0.1) -> list[Cue]:
    """``scenes`` is (start_in_final_video, voice_duration, narration)."""
    cues: list[Cue] = []
    for start, duration, narration in scenes:
        text = unicodedata.normalize("NFC", narration or "").strip()
        if not text or duration <= 0:
            continue
        chunks = chunk_lines(wrap_text(text, max_chars))
        weights = [max(1, len(c.replace("\n", " "))) for c in chunks]
        total = float(sum(weights))
        span = max(0.3, duration - tail_out)
        cursor = start + lead_in
        for chunk, weight in zip(chunks, weights, strict=True):
            length = span * weight / total
            cues.append(Cue(round(cursor, 3), round(cursor + max(0.4, length) - 0.04, 3), chunk))
            cursor += length
    return cues


def _stamp(seconds: float, sep: str) -> str:
    millis = int(round(seconds * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{ms:03d}"


def to_srt(cues: list[Cue]) -> str:
    return "\n".join(f"{i}\n{_stamp(c.start, ',')} --> {_stamp(c.end, ',')}\n{c.text}\n" for i, c in enumerate(cues, 1))


def to_vtt(cues: list[Cue]) -> str:
    body = "\n".join(f"{_stamp(c.start, '.')} --> {_stamp(c.end, '.')}\n{c.text}\n" for c in cues)
    return "WEBVTT\n\n" + body


def _ass_time(seconds: float) -> str:
    centis = int(round(seconds * 100))
    hours, rest = divmod(centis, 360_000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def to_ass(cues: list[Cue], width: int, height: int) -> str:
    sample = " ".join(c.text for c in cues)
    font = ass_font_name(sample)
    size = round(min(width, height) * 0.062 if height > width else height * 0.052)
    margin_v = round(height * (0.14 if height > width else 0.08))
    margin_h = round(width * 0.06)
    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,{max(2, size // 16)},1,2,"
        f"{margin_h},{margin_h},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    rows = [f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Default,,0,0,0,,{c.text.replace(chr(10), chr(92) + 'N')}" for c in cues]
    return header + "\n".join(rows) + "\n"


def write_all(cues: list[Cue], directory: Path, stem: str, width: int, height: int) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    files = {"srt": to_srt(cues), "vtt": to_vtt(cues), "ass": to_ass(cues, width, height)}
    paths = {}
    for suffix, content in files.items():
        path = directory / f"{stem}.{suffix}"
        path.write_text(content, encoding="utf-8")
        paths[suffix] = str(path)
    return paths


def parse_srt(text: str) -> list[Cue]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        match = re.match(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", lines[1])
        if not match:
            continue
        a = [int(x) for x in match.groups()]
        cues.append(Cue(a[0] * 3600 + a[1] * 60 + a[2] + a[3] / 1000, a[4] * 3600 + a[5] * 60 + a[6] + a[7] / 1000, "\n".join(lines[2:])))
    return cues
