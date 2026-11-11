"""Structured logging.

Every record carries the ambient execution context (execution_id, task_id, agent,
tool, provider) and passes through the secret masker before it is written.
The JSON file is the machine record; the console handler is the human view.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from .config import Settings
from .security.secrets import mask, mask_obj, set_known_secrets

_context: ContextVar[dict[str, Any]] = ContextVar("sax_log_context", default={})  # noqa: B039
_CONTEXT_KEYS = ("execution_id", "task_id", "agent", "tool", "provider")


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    merged = {**_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_context.get())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": mask(record.getMessage()),
        }
        payload.update(_context.get())
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            payload.update(mask_obj(data))
        if record.exc_info:
            payload["exc"] = mask(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = _context.get()
        where = "/".join(str(ctx[key]) for key in ("agent", "tool", "provider") if ctx.get(key))
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        prefix = f"{stamp} {record.levelname:<7}"
        line = f"{prefix} [{where}] {record.getMessage()}" if where else f"{prefix} {record.getMessage()}"
        if record.exc_info and record.levelno >= logging.ERROR:
            line += "\n" + self.formatException(record.exc_info)
        return mask(line)


def configure_logging(settings: Settings, *, console: bool = True, file: bool = True) -> None:
    set_known_secrets(settings.secret_values())
    root = logging.getLogger("superagent")
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(HumanFormatter())
        root.addHandler(stream)
    if file:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = RotatingFileHandler(
            settings.logs_dir / "superagent.jsonl", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
        )
        log_file.setFormatter(JsonFormatter())
        root.addHandler(log_file)
    for noisy in ("httpx", "httpcore", "apscheduler", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"superagent.{name}")


def read_log_tail(settings: Settings, limit: int = 200) -> list[dict[str, Any]]:
    """Last ``limit`` structured records, newest last. Used by the CLI and dashboard."""
    path = settings.logs_dir / "superagent.jsonl"
    if not path.exists():
        return []
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 400_000))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
