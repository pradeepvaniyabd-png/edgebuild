from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Time-sortable id: millisecond timestamp + random suffix."""
    return f"{prefix}_{int(time.time() * 1000):011x}{secrets.token_hex(4)}"


def stable_hash(obj: Any, length: int = 32) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def file_sha256(path: str | Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def slugify(text: str, max_len: int = 48) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()[:max_len].strip("-")
    return slug or f"item-{stable_hash(text, 8)}"


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars/token for ASCII, ~2 for Devanagari and other scripts."""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return max(1, round(ascii_chars / 4 + (len(text) - ascii_chars) / 2))


def truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - 40)
    head, tail = keep * 2 // 3, keep // 3
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…[{omitted} chars omitted]…\n{text[len(text) - tail:] if tail else ''}"


_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse the first JSON object/array in model output, tolerating fences and prose."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty response, expected JSON")
    candidates = [stripped] + [m.group(1).strip() for m in _FENCE.finditer(stripped)]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    for index, ch in enumerate(stripped):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(stripped, index)
            except json.JSONDecodeError:
                continue
            return value
    raise ValueError("no JSON value found in response")


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def deep_get(data: Any, dotted: str) -> Any:
    """Walk ``a.b.0.c`` through dicts and lists; raises KeyError when a segment is missing."""
    current = data
    for part in dotted.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(dotted)
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise KeyError(dotted) from exc
        else:
            raise KeyError(dotted)
    return current
