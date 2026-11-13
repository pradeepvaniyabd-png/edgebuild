from __future__ import annotations

import os
from pathlib import Path

import pytest

from superagent.errors import PathEscapeError, SecurityError
from superagent.security import CommandPolicy, PathSandbox, find_secrets, mask, mask_obj, redact_pii
from superagent.security.secrets import MASK, set_known_secrets


@pytest.fixture
def sandbox(tmp_path: Path) -> PathSandbox:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ro").mkdir()
    (tmp_path / "outside").mkdir()
    return PathSandbox([tmp_path / "ws"], [tmp_path / "ro"])


# ------------------------------------------------------------------ sandbox
def test_relative_paths_resolve_inside_workspace(sandbox: PathSandbox, tmp_path: Path):
    assert sandbox.resolve("a/b.txt", write=True) == (tmp_path / "ws" / "a" / "b.txt").resolve()


@pytest.mark.parametrize("bad", ["../outside/x", "a/../../outside/x", "/etc/passwd", "~/secrets.txt", ""])
def test_traversal_and_absolute_escapes_are_refused(sandbox: PathSandbox, bad: str):
    with pytest.raises(SecurityError):
        sandbox.resolve(bad)


def test_null_byte_is_refused(sandbox: PathSandbox):
    with pytest.raises(PathEscapeError):
        sandbox.resolve("a\x00b")


def test_symlink_pointing_outside_is_refused(sandbox: PathSandbox, tmp_path: Path):
    secret = tmp_path / "outside" / "secret.txt"
    secret.write_text("x")
    os.symlink(secret, tmp_path / "ws" / "link.txt")
    os.symlink(tmp_path / "outside", tmp_path / "ws" / "dirlink")
    with pytest.raises(PathEscapeError):
        sandbox.resolve("link.txt")
    with pytest.raises(PathEscapeError):
        sandbox.resolve("dirlink/secret.txt", write=True)


def test_read_root_is_read_only(sandbox: PathSandbox, tmp_path: Path):
    assert sandbox.resolve(tmp_path / "ro" / "data.csv")
    with pytest.raises(PathEscapeError):
        sandbox.resolve(tmp_path / "ro" / "data.csv", write=True)


@pytest.mark.parametrize("name", [".env", ".env.local", "id_rsa", "server.pem", ".ssh/config", ".aws/credentials"])
def test_credential_files_are_blocked_even_inside_workspace(sandbox: PathSandbox, name: str):
    with pytest.raises(SecurityError):
        sandbox.resolve(name)


# ------------------------------------------------------------------ commands
@pytest.fixture
def policy(sandbox: PathSandbox) -> CommandPolicy:
    return CommandPolicy(sandbox)


@pytest.mark.parametrize(
    "argv",
    [
        ["sudo", "ls"], ["bash", "-c", "rm -rf /"], ["sh", "script.sh"], ["env", "rm", "-rf", "x"],
        ["dd", "if=/dev/zero", "of=/dev/sda"], ["ssh", "host"], ["xargs", "rm"], ["nohup", "python", "x.py"],
        ["unknown-binary"], [""], [],
    ],
)  # fmt: skip
def test_dangerous_or_unknown_commands_are_denied(policy: CommandPolicy, argv: list[str]):
    assert policy.check(argv).action == "deny"


@pytest.mark.parametrize(
    ("argv", "category"),
    [
        (["rm", "-rf", "build"], "destructive"),
        (["python", "script.py"], "code_exec"),
        (["python3.12", "-c", "print(1)"], "code_exec"),
        (["pytest", "-q"], "code_exec"),
        (["pip", "install", "requests"], "package_install"),
        (["npm", "install"], "package_install"),
        (["curl", "https://example.com"], "network"),
        (["git", "push", "origin", "main"], "external_publish"),
        (["git", "reset", "--hard"], "destructive"),
        (["git", "clone", "https://example.com/x.git"], "network"),
        (["find", ".", "-name", "*.py", "-delete"], "destructive"),
        (["sed", "-i", "s/a/b/", "file.txt"], "code_exec"),
    ],
)
def test_risky_commands_require_approval(policy: CommandPolicy, argv: list[str], category: str):
    decision = policy.check(argv)
    assert decision.action == "approve"
    assert decision.category == category


@pytest.mark.parametrize(
    "argv",
    [["ls", "-la"], ["git", "status"], ["git", "-C", ".", "diff"], ["git", "commit", "-m", "x"],
     ["grep", "-rn", "todo", "."], ["ffprobe", "clip.mp4"], ["mkdir", "-p", "out/a"], ["find", ".", "-name", "*.py"]],
)  # fmt: skip
def test_read_only_and_local_commands_are_allowed(policy: CommandPolicy, argv: list[str]):
    assert policy.check(argv).action == "allow"


def test_paths_outside_workspace_are_denied_even_for_allowed_commands(policy: CommandPolicy, tmp_path: Path):
    assert policy.check(["cat", "/etc/passwd"]).action == "deny"
    assert policy.check(["cat", "../outside/x"]).action == "deny"
    assert policy.check(["ls", f"--directory={tmp_path / 'outside'}"]).action == "deny"
    assert policy.check(["cp", "a.txt", str(tmp_path / "ro" / "b.txt")]).action == "deny"  # read-only root
    assert policy.check(["git", "-C", "/etc", "status"]).action == "deny"


def test_git_config_injection_flags_are_denied(policy: CommandPolicy):
    assert policy.check(["git", "-c", "core.sshCommand=evil", "status"]).action == "deny"
    assert policy.check(["git", "fetch", "--upload-pack=evil"]).action == "deny"


def test_control_characters_are_denied(policy: CommandPolicy):
    assert policy.check(["ls", "a\nb"]).action == "deny"


def test_url_argument_escalates_to_network_approval(policy: CommandPolicy):
    decision = policy.check(["ffmpeg", "-i", "https://example.com/v.mp4", "out.mp4"])
    assert (decision.action, decision.category) == ("approve", "network")


def test_extra_allowlist(sandbox: PathSandbox):
    assert CommandPolicy(sandbox, ["jq"]).check(["jq", "."]).action == "allow"


# ------------------------------------------------------------------- secrets
@pytest.mark.parametrize(
    "text",
    [
        "key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz012345",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAIOSFODNN7EXAMPLE is the key id",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz.0123456789",
        "postgresql://admin:hunter2pass@db.internal:5432/app",
        "password: SuperSecret123",
        "r8_abcdefghijklmnopqrstuvwxyz0123456789abcd",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_secret_shapes_are_detected_and_masked(text: str):
    assert find_secrets(text)
    masked = mask(text)
    assert MASK in masked
    for fragment in ("AbCdEfGhIjKlMnOp", "hunter2pass", "SuperSecret123", "MIIEow", "IOSFODNN7EXAMPLE"):
        assert fragment not in masked


def test_ordinary_text_is_not_flagged():
    for text in ["The token budget is 6000", "password policy document", "task tsk_0193abcd is running", ""]:
        assert find_secrets(text) == [], text
        assert mask(text) == text


def test_configured_secret_values_are_masked_verbatim():
    set_known_secrets(["plain-looking-value-42"])
    try:
        assert mask("sending plain-looking-value-42 upstream") == f"sending {MASK} upstream"
    finally:
        set_known_secrets([])


def test_mask_obj_masks_by_key_and_by_value():
    data = {"api_key": "abc", "nested": [{"note": "password=hunter2abc"}], "max_tokens": 100, "count": 3}
    masked = mask_obj(data)
    assert masked["api_key"] == MASK
    assert MASK in masked["nested"][0]["note"]
    assert masked["max_tokens"] == 100 and masked["count"] == 3


def test_aadhaar_shaped_numbers_are_redacted():
    assert redact_pii("Aadhaar 1234 5678 9012 verified") == "Aadhaar XXXX-XXXX-9012 verified"
    assert redact_pii("mobile 9876543210, amount 123456789012345") == "mobile 9876543210, amount 123456789012345"
