"""Filesystem sandbox: every tool path resolves through here before it touches disk."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from ..errors import PathEscapeError, SecurityError

_SENSITIVE_NAMES = {
    ".netrc", ".pgpass", ".npmrc", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.json", "token.json", "secrets.json", "secrets.yaml", "secrets.yml",
}  # fmt: skip
_SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure"}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}


class PathSandbox:
    """Resolves paths and rejects anything outside the configured roots.

    ``write_roots`` are read+write; ``read_roots`` are read-only. Symlinks are
    resolved before the containment check, so a link pointing outside is refused.
    """

    def __init__(self, write_roots: Iterable[str | Path], read_roots: Iterable[str | Path] = ()):
        self.write_roots = [Path(os.path.realpath(os.fspath(p))) for p in write_roots]
        self.read_roots = [Path(os.path.realpath(os.fspath(p))) for p in read_roots]
        if not self.write_roots:
            raise ValueError("PathSandbox needs at least one write root")

    @property
    def primary(self) -> Path:
        return self.write_roots[0]

    def resolve(self, path: str | Path, *, write: bool = False, allow_sensitive: bool = False) -> Path:
        raw = os.fspath(path)
        if not raw or "\x00" in raw:
            raise PathEscapeError("empty or malformed path")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.primary / candidate
        real = Path(os.path.realpath(candidate))
        roots = self.write_roots if write else self.write_roots + self.read_roots
        if not any(real == root or root in real.parents for root in roots):
            mode = "writable" if write else "accessible"
            raise PathEscapeError(
                f"path '{raw}' is outside the {mode} workspace",
                hint="work inside the workspace, or add a read-only root via SAX_ALLOWED_ROOTS",
            )
        if not allow_sensitive and self.is_sensitive(real):
            raise SecurityError(f"access to credential-like file '{real.name}' is blocked")
        return real

    def contains(self, path: str | Path, *, write: bool = False) -> bool:
        try:
            self.resolve(path, write=write, allow_sensitive=True)
        except SecurityError:
            return False
        return True

    @staticmethod
    def is_sensitive(path: Path) -> bool:
        name = path.name.lower()
        if name in _SENSITIVE_NAMES or name == ".env" or name.startswith(".env."):
            return True
        if path.suffix.lower() in _SENSITIVE_SUFFIXES:
            return True
        return any(part.lower() in _SENSITIVE_PARTS for part in path.parts)

    def relative(self, path: Path) -> str:
        """Display form relative to the root that contains ``path``."""
        for root in self.write_roots + self.read_roots:
            if path == root or root in path.parents:
                return os.fspath(path.relative_to(root))
        return os.fspath(path)
