"""Secret detection and masking.

Used by logging (mask everything that leaves the process as text), by memory
(refuse to persist credentials) and by the API (never echo a secret back).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

MASK = "***REDACTED***"

# (kind, pattern). Quantifiers are bounded and non-overlapping to avoid catastrophic backtracking.
_TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----[\s\S]{0,8000}?-----END [A-Z ]{0,30}PRIVATE KEY-----")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,200}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,200}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,255}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,255}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,200}")),
    ("replicate_token", re.compile(r"\br8_[A-Za-z0-9]{30,100}\b")),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9]{30,100}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,2000}\.[A-Za-z0-9_\-]{10,4000}\.[A-Za-z0-9_\-]{10,2000}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.=]{16,2000}")),
    ("url_credentials", re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{1,20}://[^/\s:@]{1,100}:[^/\s:@]{1,200}@")),
]

# key = value style assignments: keep the key so logs stay readable, hide the value.
_ASSIGNMENT = re.compile(
    r"(?i)\b((?:[a-z0-9]{1,20}[_\-]){0,3}"
    r"(?:pass(?:word|wd)?|secret|api[_\-]?key|access[_\-]?token|auth[_\-]?token|refresh[_\-]?token"
    r"|client[_\-]?secret|private[_\-]?key|fal[_\-]?key|token))"
    r"(\s{0,3}[:=]\s{0,3})(['\"]?)([^\s'\",;&]{6,400})"
)

_AADHAAR = re.compile(r"(?<![\d.])\d{4}[ \-]?\d{4}[ \-]?\d{4}(?![\d.])")

_known_secrets: list[str] = []


def set_known_secrets(values: Iterable[str]) -> None:
    """Register configured secret values so they are masked verbatim, whatever their shape."""
    global _known_secrets
    _known_secrets = sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True)


def find_secrets(text: str) -> list[str]:
    """Return the kinds of secrets detected in ``text`` (empty list when clean)."""
    if not text:
        return []
    kinds: list[str] = []
    if any(secret in text for secret in _known_secrets):
        kinds.append("configured_secret")
    for kind, pattern in _TOKEN_PATTERNS:
        if pattern.search(text):
            kinds.append(kind)
    if _ASSIGNMENT.search(text):
        kinds.append("credential_assignment")
    return kinds


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))


def mask(text: str) -> str:
    if not text:
        return text
    for secret in _known_secrets:
        if secret in text:
            text = text.replace(secret, MASK)
    for _, pattern in _TOKEN_PATTERNS:
        text = pattern.sub(MASK, text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", text)


def redact_pii(text: str) -> str:
    """Mask Aadhaar-shaped 12-digit numbers, keeping the last four digits for traceability."""
    if not text:
        return text
    return _AADHAAR.sub(lambda m: "XXXX-XXXX-" + re.sub(r"\D", "", m.group(0))[-4:], text)


def mask_obj(value: Any, _depth: int = 0) -> Any:
    """Recursively mask strings inside JSON-like data."""
    if _depth > 8:
        return value
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {k: (MASK if _is_secret_key(str(k)) and v else mask_obj(v, _depth + 1)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [mask_obj(v, _depth + 1) for v in value]
    return value


_SECRET_KEY = re.compile(
    r"(?i)(pass(word|wd)?|secret|api[_\-]?key|token|authorization|private[_\-]?key|credential)"
)


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key)) and not key.lower().endswith(("_tokens", "tokens", "token_budget"))
