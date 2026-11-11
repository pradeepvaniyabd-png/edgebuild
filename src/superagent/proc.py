"""Subprocess runner shared by tools and the media layer.

Always exec (never a shell), always with a timeout, always in its own process
group so a timeout or cancellation kills the whole tree.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import DependencyMissing, TaskTimeout

_INSTALL_HINTS = {
    "ffmpeg": "install FFmpeg (apt install ffmpeg / brew install ffmpeg)",
    "ffprobe": "install FFmpeg, which ships ffprobe",
    "espeak-ng": "install espeak-ng (apt install espeak-ng) or configure another voice provider",
    "git": "install git",
}


@dataclass
class ProcessResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, chars: int = 2000) -> str:
        text = (self.stderr or self.stdout).strip()
        return text[-chars:]


def which(binary: str) -> str | None:
    return shutil.which(binary)


def require_binary(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise DependencyMissing(
            f"required program '{binary}' was not found on PATH",
            hint=_INSTALL_HINTS.get(os.path.basename(binary), f"install '{binary}'"),
        )
    return path


async def _drain(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    kept: list[bytes] = []
    size = 0
    truncated = False
    while chunk := await stream.read(65536):
        if size < limit:
            piece = chunk[: limit - size]
            kept.append(piece)
            size += len(piece)
            truncated = truncated or len(piece) < len(chunk)
        else:
            truncated = True
    return b"".join(kept), truncated


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


async def run_process(
    argv: Sequence[str],
    *,
    timeout: float = 120.0,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdin_data: bytes | None = None,
    max_output: int = 2_000_000,
    raise_on_timeout: bool = True,
) -> ProcessResult:
    args = [os.fspath(a) for a in argv]
    require_binary(args[0])
    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        start_new_session=sys.platform != "win32",
    )

    async def _communicate() -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
        if stdin_data is not None and proc.stdin is not None:
            proc.stdin.write(stdin_data)
            try:
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            proc.stdin.close()
        out, err = await asyncio.gather(_drain(proc.stdout, max_output), _drain(proc.stderr, max_output))
        await proc.wait()
        return out, err

    try:
        (stdout, out_cut), (stderr, err_cut) = await asyncio.wait_for(_communicate(), timeout=timeout)
    except TimeoutError:
        _kill_tree(proc)
        await proc.wait()
        if raise_on_timeout:
            raise TaskTimeout(f"'{os.path.basename(args[0])}' did not finish within {timeout:g}s") from None
        return ProcessResult(args, -9, "", f"timed out after {timeout:g}s", time.perf_counter() - started)
    except BaseException:
        _kill_tree(proc)
        raise
    return ProcessResult(
        argv=args,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        duration=time.perf_counter() - started,
        truncated=out_cut or err_cut,
    )


def scrubbed_env(home: str | os.PathLike[str], extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for untrusted commands: no provider keys, HOME inside the workspace."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.fspath(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
    }
    for passthrough in ("TMPDIR", "TZ", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE"):
        if passthrough in os.environ:
            env[passthrough] = os.environ[passthrough]
    if extra:
        env.update(extra)
    return env
