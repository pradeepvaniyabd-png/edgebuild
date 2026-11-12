"""`super-agent doctor`: every check returns PASS, WARN or FAIL with a concrete remediation."""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .proc import run_process, which
from .types import ProviderKind, ProviderStatus


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


def _ok(name: str, detail: str) -> Check:
    return Check(name, "PASS", detail)


async def run_doctor(platform: Any, *, frontend_dir: Path | None = None) -> list[Check]:
    s = platform.settings
    checks: list[Check] = []

    major, minor = sys.version_info[:2]
    checks.append(_ok("python", f"{major}.{minor}") if (major, minor) >= (3, 11) else Check("python", "FAIL", f"{major}.{minor}", "use Python 3.11 or newer"))

    missing = []
    for module in ("fastapi", "pydantic", "sqlalchemy", "alembic", "httpx", "typer", "apscheduler", "PIL", "openpyxl", "pypdf", "docx"):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    checks.append(Check("dependencies", "FAIL", "missing: " + ", ".join(missing), "pip install -e .") if missing else _ok("dependencies", "all core packages import"))

    try:
        await platform.db.ping()
        current = await platform.db.is_current()
        checks.append(_ok("database", f"{s.db_url.split('://')[0]} reachable") if current else Check("database", "FAIL", "schema is behind the latest migration", "super-agent db upgrade"))
        revision = await platform.db.current_revision()
        checks.append(_ok("migrations", f"at revision {revision}") if current else Check("migrations", "FAIL", f"at {revision}", "super-agent db upgrade"))
    except Exception as exc:
        checks.append(Check("database", "FAIL", f"{type(exc).__name__}: {exc}"[:200], "check SAX_DATABASE_URL and that the server is running"))
        checks.append(Check("migrations", "FAIL", "not checked because the database is unreachable"))

    for binary, why in (("ffmpeg", "rendering video"), ("ffprobe", "validating media")):
        path = which(binary)
        if path is None:
            checks.append(Check(binary, "FAIL", f"not found ({why} needs it)", "install FFmpeg: apt install ffmpeg / brew install ffmpeg"))
        else:
            version = (await run_process([binary, "-version"], timeout=20, raise_on_timeout=False)).stdout.splitlines()[:1]
            checks.append(_ok(binary, version[0][:70] if version else path))

    try:
        probe_file = s.workspace / ".doctor_write_test"
        probe_file.write_text("ok")
        probe_file.unlink()
        checks.append(_ok("filesystem", f"workspace writable: {s.workspace}"))
    except OSError as exc:
        checks.append(Check("workspace", "FAIL", str(exc)[:160], "fix permissions or set SAX_WORKSPACE_DIR"))

    health = platform.router.health()
    for kind in ProviderKind:
        group = [h for h in health if h.kind is kind]
        ready = [h.name for h in group if h.status is ProviderStatus.AVAILABLE]
        if ready:
            checks.append(_ok(f"providers:{kind.value}", ", ".join(ready)))
        else:
            hint = next((h.detail for h in group if h.status is ProviderStatus.NOT_CONFIGURED), "")
            level = "FAIL" if kind is ProviderKind.EMBEDDING else "WARN"
            checks.append(Check(f"providers:{kind.value}", level, "none configured", hint or "see `super-agent providers`"))

    agents = platform.agents.names()
    expected = {"master", "research", "coder", "debugger", "file", "analyst", "government", "trading", "system", "director", "imager", "videographer", "voice"}
    absent = sorted(expected - set(agents))
    checks.append(Check("agent_registry", "FAIL", f"missing: {', '.join(absent)}") if absent else _ok("agent_registry", f"{len(agents)} agents"))
    checks.append(_ok("tool_registry", f"{len(platform.tools.names())} tools") if len(platform.tools.names()) >= 15 else Check("tool_registry", "FAIL", "too few tools registered"))

    checks.append(_ok("scheduler", "running") if platform.scheduler is not None and platform.scheduler.started else Check("scheduler", "WARN", "not running in this process", "it starts with `super-agent serve`"))

    try:
        saved = await platform.memory.save("doctor memory round-trip probe", kind="fact", scope="doctor")
        found = await platform.memory.search("doctor memory round-trip", scopes=["doctor"], min_score=0.05)
        await platform.memory.delete(saved["id"])
        checks.append(_ok("memory", "save/search/delete works") if found else Check("memory", "FAIL", "saved memory was not found by search"))
    except Exception as exc:
        checks.append(Check("memory", "FAIL", f"{type(exc).__name__}: {exc}"[:160]))

    from .errors import SecurityError

    escapes = 0
    for bad in ("/etc/passwd", "../../etc/passwd", ".env"):
        try:
            platform.sandbox.resolve(bad)
        except SecurityError:
            escapes += 1
    verdict = platform.policy.check(["sudo", "ls"]).action == "deny" and platform.policy.check(["rm", "-rf", "x"]).action == "approve"
    checks.append(_ok("permissions", "path sandbox and command policy enforce their rules") if escapes == 3 and verdict else Check("permissions", "FAIL", "sandbox or command policy is not enforcing", "report this as a bug"))
    checks.append(_ok("approvals", f"mode={s.approval_mode}") if s.approval_mode != "deny" else Check("approvals", "WARN", "mode=deny blocks every risky action", "set SAX_APPROVAL_MODE=queue"))

    try:
        import httpx

        from .api.app import create_app

        app = create_app(platform)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://doctor") as client:
            response = await client.get("/health")
        checks.append(_ok("api", "GET /health -> 200") if response.status_code == 200 else Check("api", "FAIL", f"/health returned {response.status_code}"))
    except Exception as exc:
        checks.append(Check("api", "FAIL", f"{type(exc).__name__}: {exc}"[:160]))

    ui = frontend_dir or Path(__file__).resolve().parents[2] / "frontend"
    if (ui / "dist" / "index.html").is_file():
        checks.append(_ok("frontend_build", "frontend/dist is built"))
    elif ui.is_dir():
        checks.append(Check("frontend_build", "WARN", "dashboard is not built", "cd frontend && npm install && npm run build"))
    else:
        checks.append(Check("frontend_build", "WARN", "frontend directory not found (running from an installed package)"))

    tests = Path(__file__).resolve().parents[2] / "tests"
    try:
        importlib.import_module("pytest")
        checks.append(_ok("tests", "pytest installed; run `pytest`") if tests.is_dir() else Check("tests", "WARN", "test suite not found next to the package"))
    except ImportError:
        checks.append(Check("tests", "WARN", "pytest is not installed", "pip install -e '.[dev]'"))

    if s.is_sqlite and os.environ.get("SAX_ENV") == "production":
        checks.append(Check("production_db", "WARN", "SQLite is in use with SAX_ENV=production", "use PostgreSQL via SAX_DATABASE_URL"))
    return checks


def summarize(checks: list[Check]) -> dict[str, int]:
    return {level: sum(1 for c in checks if c.status == level) for level in ("PASS", "WARN", "FAIL")}
