from __future__ import annotations

from pathlib import Path

from superagent.security.audit import run_audit


def _by_check(findings):
    return {f.check: f for f in findings}


async def test_audit_clean_defaults(platform, tmp_path: Path):
    found = _by_check(run_audit(platform, project_root=tmp_path))
    assert found["path_sandbox"].severity == "OK"
    assert found["command_policy"].severity == "OK"
    assert found["child_env"].severity == "OK"
    assert found["api_auth"].severity == "LOW"  # loopback without token is advisory only
    assert not any(f.severity == "HIGH" for f in found.values())


async def test_audit_flags_public_bind_without_token(platform, tmp_path: Path):
    platform.settings.api_host = "0.0.0.0"  # noqa: S104
    assert _by_check(run_audit(platform, project_root=tmp_path))["api_auth"].severity == "HIGH"


async def test_audit_finds_planted_secret(platform, tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "leak.py").write_text('KEY = "ghp_' + "a1B2c3D4e5" * 4 + '"\n')
    found = _by_check(run_audit(platform, project_root=tmp_path))
    assert found["hardcoded_secrets"].severity == "HIGH"
    assert "leak.py" in found["hardcoded_secrets"].detail
    assert "ghp_" not in found["hardcoded_secrets"].detail  # the report never repeats the secret


async def test_sqlite_files_are_owner_only(platform):
    import os
    import stat

    path = platform.settings.home_path / "superagent.db"
    if path.exists() and os.name == "posix":
        assert not (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
