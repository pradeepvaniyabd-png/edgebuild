"""``{{placeholder}}`` substitution for provider input templates and ComfyUI workflows.

A value that is exactly one placeholder keeps the variable's type (so ``"{{seed}}"``
becomes an int); placeholders inside longer strings are substituted as text.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..errors import ConfigError

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def fill_template(template: Any, variables: dict[str, Any]) -> Any:
    if isinstance(template, dict):
        return {key: fill_template(value, variables) for key, value in template.items()}
    if isinstance(template, list):
        return [fill_template(item, variables) for item in template]
    if not isinstance(template, str) or "{{" not in template:
        return template
    whole = _PLACEHOLDER.fullmatch(template.strip())
    if whole:
        return variables.get(whole.group(1))
    return _PLACEHOLDER.sub(lambda m: "" if variables.get(m.group(1)) is None else str(variables[m.group(1)]), template)


def placeholders(template: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(template, dict):
        for value in template.values():
            found |= placeholders(value)
    elif isinstance(template, list):
        for item in template:
            found |= placeholders(item)
    elif isinstance(template, str):
        found |= set(_PLACEHOLDER.findall(template))
    return found


def parse_json_setting(raw: str | None, setting: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{setting} must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{setting} must be a JSON object")
    return value


def drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


_ASPECTS = {
    "1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "3:2": 3 / 2, "2:3": 2 / 3,
    "4:5": 4 / 5, "5:4": 5 / 4, "21:9": 21 / 9, "9:21": 9 / 21,
}  # fmt: skip


def nearest_aspect(width: int, height: int, allowed: list[str] | None = None) -> str:
    target = width / height if height else 1.0
    options = {name: _ASPECTS[name] for name in (allowed or _ASPECTS) if name in _ASPECTS}
    return min(options, key=lambda name: abs(options[name] - target))
