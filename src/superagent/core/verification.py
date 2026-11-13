"""Verification engine: a result only counts once its checks pass.

A check spec is a small dict, e.g. ``{"type": "file_exists", "key": "path", "min_bytes": 100}``.
``key`` reads a dotted path from the task output; ``path``/``value`` give a literal instead.
"""

from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import SuperAgentError
from ..media.probe import probe, probe_image
from ..utils import deep_get, file_sha256, human_size

_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,), "int": (int,), "number": (int, float), "bool": (bool,),
    "list": (list,), "dict": (dict,),
}  # fmt: skip


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail, "skipped": self.skipped}


@dataclass
class VerificationReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed or check.skipped for check in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed and not check.skipped]

    def summary(self) -> str:
        if not self.checks:
            return "no checks declared"
        ok = sum(1 for c in self.checks if c.passed)
        skipped = sum(1 for c in self.checks if c.skipped)
        text = f"{ok}/{len(self.checks) - skipped} checks passed"
        if skipped:
            text += f", {skipped} skipped"
        if self.failures:
            text += " — failed: " + "; ".join(f"{c.name} ({c.detail})" for c in self.failures)
        return text

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "summary": self.summary(), "checks": [c.to_dict() for c in self.checks]}


Check = Callable[[dict[str, Any], dict[str, Any]], Awaitable[CheckResult]]


class _Missing(Exception):
    pass


def _value(spec: dict[str, Any], output: dict[str, Any], literal: str = "value") -> Any:
    if literal in spec:
        return spec[literal]
    key = spec.get("key")
    if key is None:
        raise _Missing("check needs 'key' or a literal")
    try:
        return deep_get(output, key)
    except KeyError:
        raise _Missing(f"output has no field '{key}'") from None


def _paths(spec: dict[str, Any], output: dict[str, Any]) -> list[Path]:
    raw = _value(spec, output, literal="path")
    if raw is None:
        raise _Missing(f"'{spec.get('key')}' is empty")
    items = raw if isinstance(raw, list) else [raw]
    if not items:
        raise _Missing("no files to check")
    return [Path(str(item)) for item in items]


class VerificationEngine:
    def __init__(self) -> None:
        self._checks: dict[str, Check] = {
            "schema": self._schema,
            "non_empty": self._non_empty,
            "file_exists": self._file_exists,
            "checksum": self._checksum,
            "image": self._image,
            "media": self._media,
            "row_count": self._count,
            "list_min": self._count,
            "python_syntax": self._python_syntax,
            "exit_code": self._equals,
            "equals": self._equals,
            "one_of": self._one_of,
            "contains": self._contains,
        }

    def register(self, name: str, check: Check) -> None:
        self._checks[name] = check

    async def verify(self, specs: list[dict[str, Any]], output: dict[str, Any]) -> VerificationReport:
        report = VerificationReport()
        for spec in specs:
            kind = spec.get("type", "?")
            name = spec.get("name") or f"{kind}:{spec.get('key', spec.get('path', ''))}".rstrip(":")
            check = self._checks.get(kind)
            if check is None:
                report.checks.append(CheckResult(name, False, f"unknown check type '{kind}'"))
                continue
            try:
                result = await check(spec, output)
                result.name = name
            except _Missing as exc:
                if spec.get("optional"):
                    result = CheckResult(name, True, f"skipped: {exc}", skipped=True)
                else:
                    result = CheckResult(name, False, str(exc))
            except SuperAgentError as exc:
                result = CheckResult(name, False, exc.message)
            except Exception as exc:  # a broken check must fail closed, not crash the task
                result = CheckResult(name, False, f"check crashed: {type(exc).__name__}: {exc}")
            report.checks.append(result)
        return report

    # --------------------------------------------------------------- checks
    async def _schema(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        target = _value(spec, output) if "key" in spec else output
        if not isinstance(target, dict):
            return CheckResult("", False, "expected an object")
        missing = [key for key in spec.get("required", []) if target.get(key) in (None, "", [], {})]
        if missing:
            return CheckResult("", False, f"missing or empty: {', '.join(missing)}")
        for key, type_name in (spec.get("types") or {}).items():
            if key in target and target[key] is not None:
                expected = _TYPES.get(type_name)
                value = target[key]
                if expected and (not isinstance(value, expected) or (type_name != "bool" and isinstance(value, bool))):
                    return CheckResult("", False, f"'{key}' should be {type_name}, got {type(value).__name__}")
        return CheckResult("", True, "structure ok")

    async def _non_empty(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        value = _value(spec, output)
        size = len(value) if hasattr(value, "__len__") else (0 if value is None else 1)
        minimum = int(spec.get("min_chars", spec.get("min", 1)))
        if isinstance(value, str):
            size = len(value.strip())
        return CheckResult("", size >= minimum, f"length {size} (min {minimum})")

    async def _file_exists(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        minimum = int(spec.get("min_bytes", 1))
        sizes = []
        for path in _paths(spec, output):
            if not path.is_file():
                return CheckResult("", False, f"missing file {path.name}")
            size = path.stat().st_size
            if size < minimum:
                return CheckResult("", False, f"{path.name} is {size} bytes (min {minimum})")
            sizes.append(size)
        return CheckResult("", True, f"{len(sizes)} file(s), {human_size(sum(sizes))}")

    async def _checksum(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        path = _paths(spec, output)[0]
        if not path.is_file():
            return CheckResult("", False, f"missing file {path.name}")
        actual = file_sha256(path)
        expected = str(spec.get("sha256", "")).lower()
        return CheckResult("", actual == expected, f"sha256 {actual[:12]}… vs expected {expected[:12]}…")

    async def _image(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        details = []
        for path in _paths(spec, output):
            info = probe_image(path)
            if "width" in spec and info.width != int(spec["width"]):
                return CheckResult("", False, f"{path.name} width {info.width} != {spec['width']}")
            if "height" in spec and info.height != int(spec["height"]):
                return CheckResult("", False, f"{path.name} height {info.height} != {spec['height']}")
            if info.width < int(spec.get("min_width", 1)) or info.height < int(spec.get("min_height", 1)):
                return CheckResult("", False, f"{path.name} is only {info.width}x{info.height}")
            if "aspect_ratio" in spec:
                tolerance = float(spec.get("aspect_tolerance", 0.03))
                if abs(info.aspect_ratio - float(spec["aspect_ratio"])) > tolerance:
                    return CheckResult("", False, f"{path.name} aspect {info.aspect_ratio:.3f} != {spec['aspect_ratio']}")
            details.append(f"{info.width}x{info.height} {info.format}")
        return CheckResult("", True, ", ".join(details[:4]))

    async def _media(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        details = []
        for path in _paths(spec, output):
            info = await probe(path)
            kind = spec.get("kind", "video")
            if kind == "video" and not info.has_video:
                return CheckResult("", False, f"{path.name} has no video stream")
            if kind == "audio" and not info.has_audio:
                return CheckResult("", False, f"{path.name} has no audio stream")
            if spec.get("has_audio") and not info.has_audio:
                return CheckResult("", False, f"{path.name} has no audio stream")
            if info.duration < float(spec.get("min_duration", 0.05)):
                return CheckResult("", False, f"{path.name} duration {info.duration:.2f}s is too short")
            if "max_duration" in spec and info.duration > float(spec["max_duration"]):
                return CheckResult("", False, f"{path.name} duration {info.duration:.2f}s exceeds {spec['max_duration']}s")
            if "duration" in spec:
                tolerance = float(spec.get("duration_tolerance", 0.5))
                if abs(info.duration - float(spec["duration"])) > tolerance:
                    return CheckResult("", False, f"{path.name} duration {info.duration:.2f}s != {spec['duration']}s ±{tolerance}")
            if info.video is not None:
                for dim in ("width", "height"):
                    if dim in spec and getattr(info.video, dim) != int(spec[dim]):
                        return CheckResult("", False, f"{path.name} {dim} {getattr(info.video, dim)} != {spec[dim]}")
                if "fps" in spec and abs(info.video.fps - float(spec["fps"])) > 0.5:
                    return CheckResult("", False, f"{path.name} fps {info.video.fps:.2f} != {spec['fps']}")
                if "codec" in spec and info.video.codec != spec["codec"]:
                    return CheckResult("", False, f"{path.name} codec {info.video.codec} != {spec['codec']}")
            details.append(f"{path.name} {info.duration:.2f}s")
        return CheckResult("", True, ", ".join(details[:4]))

    async def _count(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        value = _value(spec, output)
        count = value if isinstance(value, int) and not isinstance(value, bool) else len(value)
        if "equals" in spec and count != int(spec["equals"]):
            return CheckResult("", False, f"count {count} != {spec['equals']}")
        if count < int(spec.get("min", 0)):
            return CheckResult("", False, f"count {count} < min {spec.get('min')}")
        if "max" in spec and count > int(spec["max"]):
            return CheckResult("", False, f"count {count} > max {spec['max']}")
        return CheckResult("", True, f"count {count}")

    async def _python_syntax(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        if "path" in spec or str(spec.get("key", "")).endswith("path"):
            source = _paths(spec, output)[0].read_text(encoding="utf-8")
        else:
            source = str(_value(spec, output, literal="code"))
        try:
            ast.parse(source)
        except SyntaxError as exc:
            return CheckResult("", False, f"syntax error line {exc.lineno}: {exc.msg}")
        return CheckResult("", True, "parses")

    async def _equals(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        try:
            actual = deep_get(output, spec["key"])
        except KeyError:
            raise _Missing(f"output has no field '{spec['key']}'") from None
        expected = spec.get("equals", 0 if spec.get("type") == "exit_code" else None)
        return CheckResult("", actual == expected, f"{spec['key']}={actual!r} (expected {expected!r})")

    async def _one_of(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        actual = _value(spec, output)
        return CheckResult("", actual in spec.get("values", []), f"{actual!r} in {spec.get('values')}")

    async def _contains(self, spec: dict[str, Any], output: dict[str, Any]) -> CheckResult:
        text = str(_value(spec, output)).lower()
        needles = [str(n).lower() for n in spec.get("needles", [])]
        hits = [n for n in needles if n in text]
        ok = bool(hits) if spec.get("mode") == "any" else len(hits) == len(needles)
        missing = [n for n in needles if n not in hits]
        return CheckResult("", ok, "all present" if ok else f"missing: {', '.join(missing[:5])}")
