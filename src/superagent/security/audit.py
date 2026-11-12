"""`super-agent audit`: security posture checks that return concrete findings, never guesses."""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import SecurityError
from .secrets import find_secrets

_SKIP_DIRS = {"tests", ".git", "node_modules", ".venv", "dist", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
_TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json", ".md", ".toml", ".ini", ".yml", ".yaml", ".env", ".txt", ".cfg", ".sh", ".html"}


@dataclass
class Finding:
    severity: str  # HIGH | MEDIUM | LOW | OK
    check: str
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "check": self.check, "detail": self.detail, "fix": self.fix}


def _scan_tree(root: Path, limit: int = 4000) -> list[str]:
    hits: list[str] = []
    seen = 0
    for path in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts) or not path.is_file():
            continue
        if path.suffix not in _TEXT_SUFFIXES and path.name != ".env":
            continue
        seen += 1
        if seen > limit:
            break
        try:
            if path.stat().st_size > 1_000_000:
                continue
            kinds = [k for k in find_secrets(path.read_text(errors="ignore")) if k != "credential_assignment"]
        except OSError:
            continue
        if kinds:
            hits.append(f"{path.relative_to(root)} ({', '.join(sorted(set(kinds)))})")
    return hits


def _tracked_env_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, timeout=20, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [f for f in out.splitlines() if Path(f).name.startswith(".env") and not f.endswith((".example", ".sample"))]


def run_audit(platform: Any, *, project_root: Path | None = None) -> list[Finding]:
    s = platform.settings
    findings: list[Finding] = []
    loopback = s.api_host in {"127.0.0.1", "localhost", "::1"}
    has_token = bool(s.api_token and s.api_token.get_secret_value())

    if not loopback and not has_token:
        findings.append(Finding("HIGH", "api_auth", f"API listens on {s.api_host} with no token", "set SAX_API_TOKEN or bind to 127.0.0.1"))
    elif not has_token:
        findings.append(Finding("LOW", "api_auth", "API is loopback-only and has no token", "set SAX_API_TOKEN if other local users share this machine"))
    else:
        findings.append(Finding("OK", "api_auth", "API token required"))

    origins = s.cors_origin_list
    findings.append(
        Finding("HIGH", "cors", "wildcard CORS origin", "list explicit origins in SAX_CORS_ORIGINS")
        if "*" in origins else Finding("OK", "cors", f"{len(origins)} explicit origin(s)")
    )  # fmt: skip

    escapes = []
    for bad in ("../../etc/passwd", "/etc/shadow", ".env", "~/.ssh/id_rsa", "a/../../x"):
        try:
            platform.sandbox.resolve(bad)
            escapes.append(bad)
        except SecurityError:
            pass
    findings.append(Finding("HIGH", "path_sandbox", "escapes allowed: " + ", ".join(escapes)) if escapes else Finding("OK", "path_sandbox", "traversal, absolute and secret paths refused"))

    allowed_bad = [c for c in (["sudo", "ls"], ["mkfs", "/dev/sda"], ["dd", "if=/dev/zero", "of=/dev/sda"]) if platform.policy.check(c).action == "allow"]
    findings.append(Finding("HIGH", "command_policy", f"dangerous commands allowed: {allowed_bad}") if allowed_bad else Finding("OK", "command_policy", "destructive and privileged commands are denied or gated"))

    findings.append(
        Finding("MEDIUM", "approvals", "approval mode is auto-approve", "use SAX_APPROVAL_MODE=queue")
        if s.approval_mode == "auto" else Finding("OK", "approvals", f"mode={s.approval_mode}")
    )  # fmt: skip

    home = s.home_path
    for name in ("superagent.db", ".env"):
        target = home / name
        if target.exists() and os.name == "posix" and target.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            findings.append(Finding("MEDIUM", "file_permissions", f"{target} is readable by other users", f"chmod 600 {target}"))
    if not any(f.check == "file_permissions" for f in findings):
        findings.append(Finding("OK", "file_permissions", "state files are private"))

    child_leak = [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "REPLICATE_API_TOKEN") if k in _child_env()]
    findings.append(Finding("HIGH", "child_env", f"secrets leak into subprocesses: {child_leak}") if child_leak else Finding("OK", "child_env", "subprocess environment is scrubbed"))

    root = project_root or Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").is_file():
        tracked = _tracked_env_files(root)
        findings.append(Finding("HIGH", "tracked_env", f"env files in git: {tracked}", "git rm --cached and rotate the keys") if tracked else Finding("OK", "tracked_env", "no .env committed"))
        hits = _scan_tree(root)
        findings.append(Finding("HIGH", "hardcoded_secrets", "; ".join(hits[:8]), "remove and rotate") if hits else Finding("OK", "hardcoded_secrets", "no credential-shaped strings in the source tree"))
    return findings


def _child_env() -> dict[str, str]:
    from ..proc import scrubbed_env

    saved = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "REPLICATE_API_TOKEN")}
    try:
        for k in saved:
            os.environ[k] = "probe-value"
        return scrubbed_env(Path.home())
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
