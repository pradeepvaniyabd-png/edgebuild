"""Command policy for ``shell.execute``.

Commands never go through a shell: argv is executed directly, so quoting tricks,
pipes and substitutions have no effect. This policy decides, per argv, whether
to allow, require human approval, or refuse outright.

It is defence in depth, not a jail: an allowed interpreter can still do anything
the OS user can, which is why interpreters always require approval.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from ..errors import SecurityError
from .sandbox import PathSandbox

Action = Literal["allow", "approve", "deny"]


@dataclass(frozen=True)
class CommandDecision:
    action: Action
    reason: str
    category: str | None = None


# Never run, even with approval: privilege escalation, raw devices, shells and wrappers
# that would launder another command past this policy.
_NEVER = {
    "sudo", "su", "doas", "pkexec", "mkfs", "fdisk", "parted", "dd", "shutdown", "reboot", "halt",
    "poweroff", "init", "systemctl", "service", "mount", "umount", "chroot", "iptables", "nft",
    "nc", "ncat", "netcat", "telnet", "ssh", "scp", "sftp", "crontab", "passwd", "useradd",
    "userdel", "usermod", "visudo", "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh",
    "env", "xargs", "nohup", "setsid", "timeout", "nice", "ionice", "time", "watch", "strace",
    "gdb", "eval", "exec", "source",
}  # fmt: skip

_CODE_EXEC = {
    "python", "python3", "pytest", "node", "deno", "bun", "ruby", "perl", "php", "make", "cargo",
    "go", "java", "javac", "npm", "npx", "pnpm", "yarn", "uv", "tox", "nox", "sed", "awk", "gawk",
}  # fmt: skip
_DESTRUCTIVE = {"rm", "rmdir", "mv", "chmod", "chown", "truncate", "shred", "ln", "kill", "pkill", "killall", "tar", "unzip"}  # fmt: skip
_NETWORK = {"curl", "wget"}
_PACKAGE = {"pip", "pip3", "apt", "apt-get", "brew", "conda", "gem"}
_WRITERS = {"cp", "mkdir", "touch", "tee", "zip"}
_READ_ONLY = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "echo", "pwd", "date", "file", "stat", "du",
    "df", "sort", "uniq", "cut", "tr", "diff", "cmp", "which", "tree", "basename", "dirname",
    "realpath", "sha256sum", "md5sum", "ruff", "mypy", "ffmpeg", "ffprobe", "espeak-ng", "fc-list",
    "find", "git",
}  # fmt: skip

_GIT_READ = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog", "grep"}  # fmt: skip
_GIT_SAFE_WRITE = {"add", "commit", "init"}
_GIT_REMOTE = {"push", "pull", "fetch", "clone", "submodule", "remote"}
_GIT_BLOCKED_FLAGS = ("-c", "--exec-path", "--upload-pack", "--receive-pack", "--config-env")
_FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}

_PYTHON_BIN = re.compile(r"^python3?(\.\d{1,2})?$")
_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]{1,20}://")
_PACKAGE_SUBCOMMANDS = {"install", "add", "remove", "uninstall", "update", "upgrade", "ci", "i", "sync"}


class CommandPolicy:
    def __init__(self, sandbox: PathSandbox, extra_allowed: Iterable[str] = ()):
        self.sandbox = sandbox
        self.extra_allowed = {name.strip() for name in extra_allowed if name.strip()}

    def check(self, argv: Sequence[str], cwd: str | os.PathLike[str] | None = None) -> CommandDecision:
        if not argv or not argv[0].strip():
            return CommandDecision("deny", "empty command")
        if any("\x00" in arg or "\n" in arg or "\r" in arg for arg in argv):
            return CommandDecision("deny", "control characters in arguments")

        name = os.path.basename(argv[0])
        args = list(argv[1:])
        if _PYTHON_BIN.match(name):
            name = "python"

        if name in _NEVER:
            return CommandDecision("deny", f"'{name}' is never allowed (shell, wrapper or privileged tool)")

        decision = self._classify(name, args)
        if decision.action == "deny":
            return decision

        needs_write = name in _DESTRUCTIVE or name in _WRITERS
        try:
            self._check_paths(args, cwd, write=needs_write)
        except SecurityError as exc:
            return CommandDecision("deny", str(exc))

        if decision.action == "allow" and any(_URL.match(arg) for arg in args):
            return CommandDecision("approve", "command references a network URL", "network")
        return decision

    # ------------------------------------------------------------------ rules
    def _classify(self, name: str, args: list[str]) -> CommandDecision:
        if name in _PACKAGE:
            return CommandDecision("approve", f"'{name}' installs or changes packages", "package_install")
        if name in _CODE_EXEC:
            if name in {"npm", "pnpm", "yarn", "uv"} and args and args[0] in _PACKAGE_SUBCOMMANDS:
                return CommandDecision("approve", f"'{name} {args[0]}' changes packages", "package_install")
            return CommandDecision("approve", f"'{name}' executes arbitrary code", "code_exec")
        if name in _DESTRUCTIVE:
            return CommandDecision("approve", f"'{name}' can delete or overwrite data", "destructive")
        if name in _NETWORK:
            return CommandDecision("approve", f"'{name}' sends data over the network", "network")
        if name == "git":
            return self._classify_git(args)
        if name == "find" and any(arg in _FIND_ACTIONS for arg in args):
            return CommandDecision("approve", "find with an action flag can delete or execute", "destructive")
        if name in _WRITERS or name in _READ_ONLY or name in self.extra_allowed:
            return CommandDecision("allow", "allowlisted")
        return CommandDecision(
            "deny", f"'{name}' is not allowlisted (extend with SAX_SHELL_ALLOW if you trust it)"
        )

    @staticmethod
    def _classify_git(args: list[str]) -> CommandDecision:
        for arg in args:
            if arg in _GIT_BLOCKED_FLAGS or arg.startswith(("--exec-path=", "--upload-pack=", "--receive-pack=")):
                return CommandDecision("deny", f"git flag '{arg}' can run arbitrary programs")
        sub = next((arg for arg in _skip_git_globals(args) if not arg.startswith("-")), None)
        if sub is None:
            return CommandDecision("allow", "git without subcommand")
        if sub in _GIT_READ:
            return CommandDecision("allow", "read-only git command")
        if sub in _GIT_SAFE_WRITE:
            return CommandDecision("allow", "local git write")
        if sub in _GIT_REMOTE:
            category = "external_publish" if sub == "push" else "network"
            return CommandDecision("approve", f"'git {sub}' talks to a remote", category)
        return CommandDecision("approve", f"'git {sub}' can rewrite or discard work", "destructive")

    def _check_paths(self, args: list[str], cwd: str | os.PathLike[str] | None, *, write: bool) -> None:
        base = os.fspath(cwd) if cwd else os.fspath(self.sandbox.primary)
        for arg in args:
            value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
            if not value or value.startswith("-") or _URL.match(value):
                continue
            looks_like_path = (
                value.startswith(("/", "~", "./", "../")) or value == ".." or "/.." in value or "/" in value
            )
            candidate = value if os.path.isabs(os.path.expanduser(value)) else os.path.join(base, value)
            if looks_like_path or os.path.lexists(candidate):
                self.sandbox.resolve(candidate, write=write)


def _skip_git_globals(args: list[str]) -> list[str]:
    """Drop ``-C <path>`` style global options so the real subcommand is found."""
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in {"-C", "--git-dir", "--work-tree", "--namespace"}:
            skip = True
            continue
        out.append(arg)
    return out
