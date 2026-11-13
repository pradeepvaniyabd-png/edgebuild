"""Font lookup for text rendering (thumbnails) and subtitle styling."""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from ..lang import has_devanagari

_CANDIDATES = {
    "devanagari-bold": ["/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", "/usr/share/fonts/noto/NotoSansDevanagari-Bold.ttf",
                        "/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc", "C:/Windows/Fonts/Nirmala.ttf"],
    "devanagari": ["/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", "/usr/share/fonts/noto/NotoSansDevanagari-Regular.ttf",
                   "/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc", "C:/Windows/Fonts/Nirmala.ttf"],
    "latin-bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                   "/System/Library/Fonts/Helvetica.ttc", "C:/Windows/Fonts/arialbd.ttf"],
    "latin": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
              "/System/Library/Fonts/Helvetica.ttc", "C:/Windows/Fonts/arial.ttf"],
}  # fmt: skip


@lru_cache(maxsize=16)
def find_font(text_sample: str, bold: bool = True) -> Path | None:
    key = ("devanagari" if has_devanagari(text_sample) else "latin") + ("-bold" if bold else "")
    for candidate in _CANDIDATES[key]:
        if Path(candidate).is_file():
            return Path(candidate)
    if shutil.which("fc-match"):  # last resort: ask fontconfig
        pattern = ":lang=hi" if has_devanagari(text_sample) else ""
        result = subprocess.run(["fc-match", "-f", "%{file}", pattern + (":bold" if bold else "")], capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and Path(result.stdout.strip()).is_file():
            return Path(result.stdout.strip())
    return None


def ass_font_name(text_sample: str) -> str:
    return "Noto Sans Devanagari" if has_devanagari(text_sample) else "DejaVu Sans"
