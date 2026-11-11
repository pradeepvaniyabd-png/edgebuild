"""Filesystem, shell, Python and git tools. All paths go through the sandbox."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import sys
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..errors import CommandBlockedError, FileProblem, SecurityError
from ..proc import ResourceLimits, run_process, scrubbed_env
from ..types import PermissionLevel, RiskLevel
from .registry import ApprovalNeed, ToolContext, ToolRegistry, ToolSpec

# Neutralise repo-controlled programs (hooks, fsmonitor, external diff) on every git call.
GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "diff.external=", "-c", "protocol.ext.allow=never"]


# ------------------------------------------------------------------ filesystem
class ReadInput(BaseModel):
    path: str
    max_bytes: int = Field(200_000, ge=1, le=5_000_000)


async def fs_read(args: ReadInput, ctx: ToolContext) -> dict[str, Any]:
    path = ctx.sandbox.resolve(args.path)
    if not path.is_file():
        raise FileProblem(f"not a file: {args.path}")
    size = path.stat().st_size
    with open(path, "rb") as handle:
        data = handle.read(args.max_bytes)
    if b"\x00" in data[:8192]:
        return {"path": str(path), "binary": True, "size": size, "text": "", "truncated": False}
    return {
        "path": str(path), "binary": False, "size": size, "truncated": size > len(data),
        "text": data.decode("utf-8", errors="replace"),
    }  # fmt: skip


class WriteInput(BaseModel):
    path: str
    content: str
    append: bool = False
    overwrite: bool = True


async def fs_write(args: WriteInput, ctx: ToolContext) -> dict[str, Any]:
    path = ctx.sandbox.resolve(args.path, write=True)
    if path.is_dir():
        raise FileProblem(f"'{args.path}' is a directory")
    existed = path.exists()
    if existed and not (args.overwrite or args.append):
        raise FileProblem(f"'{args.path}' already exists", hint="pass overwrite=true to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if args.append else "w", encoding="utf-8") as handle:
        handle.write(args.content)
    return {"path": str(path), "bytes": path.stat().st_size, "created": not existed}


class ListInput(BaseModel):
    path: str = "."
    pattern: str = "*"
    recursive: bool = False
    max_entries: int = Field(500, ge=1, le=5000)


async def fs_list(args: ListInput, ctx: ToolContext) -> dict[str, Any]:
    base = ctx.sandbox.resolve(args.path)
    if not base.is_dir():
        raise FileProblem(f"not a directory: {args.path}")
    if ".." in args.pattern or args.pattern.startswith("/"):
        raise SecurityError("pattern may not leave the listed directory")
    entries: list[dict[str, Any]] = []
    truncated = False
    for item in sorted(base.rglob(args.pattern) if args.recursive else base.glob(args.pattern)):
        if not ctx.sandbox.contains(item):  # e.g. a symlink that points outside
            continue
        if len(entries) >= args.max_entries:
            truncated = True
            break
        stat = item.lstat()
        entries.append({
            "name": os.fspath(item.relative_to(base)),
            "type": "dir" if item.is_dir() else "file",
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
        })  # fmt: skip
    return {"path": str(base), "entries": entries, "truncated": truncated}


class MkdirInput(BaseModel):
    path: str


async def fs_mkdir(args: MkdirInput, ctx: ToolContext) -> dict[str, Any]:
    path = ctx.sandbox.resolve(args.path, write=True)
    path.mkdir(parents=True, exist_ok=True)
    return {"path": str(path)}


class MoveInput(BaseModel):
    source: str
    destination: str
    overwrite: bool = False


async def fs_move(args: MoveInput, ctx: ToolContext) -> dict[str, Any]:
    source = ctx.sandbox.resolve(args.source, write=True)
    destination = ctx.sandbox.resolve(args.destination, write=True)
    if not source.exists():
        raise FileProblem(f"not found: {args.source}")
    if destination.exists() and not args.overwrite:
        raise FileProblem(f"'{args.destination}' already exists", hint="pass overwrite=true to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(os.fspath(source), os.fspath(destination))
    return {"source": str(source), "destination": str(destination)}


class DeleteInput(BaseModel):
    path: str
    recursive: bool = False


def _delete_guard(args: DeleteInput, ctx: ToolContext) -> ApprovalNeed:
    path = ctx.sandbox.resolve(args.path, write=True)
    if path in ctx.sandbox.write_roots:
        raise SecurityError("refusing to delete a workspace root")
    return ApprovalNeed(
        "file_delete", f"permanently delete {ctx.sandbox.relative(path)}",
        {"path": ctx.sandbox.relative(path), "recursive": args.recursive},
    )  # fmt: skip


async def fs_delete(args: DeleteInput, ctx: ToolContext) -> dict[str, Any]:
    path = ctx.sandbox.resolve(args.path, write=True)
    if not path.exists() and not path.is_symlink():
        raise FileProblem(f"not found: {args.path}")
    if path.is_dir() and not path.is_symlink():
        if args.recursive:
            shutil.rmtree(path)
        else:
            path.rmdir()
    else:
        path.unlink()
    return {"deleted": str(path)}


# ----------------------------------------------------------------------- shell
class ShellInput(BaseModel):
    argv: list[str] = Field(default_factory=list)
    command: str | None = None
    cwd: str = "."
    timeout: float = Field(120.0, gt=0, le=3600)
    stdin: str | None = None

    @model_validator(mode="after")
    def _split(self) -> ShellInput:
        if not self.argv and self.command:
            try:
                self.argv = shlex.split(self.command)  # tokenised only: no shell ever interprets it
            except ValueError as exc:
                raise ValueError(f"cannot parse command: {exc}") from exc
        if not self.argv:
            raise ValueError("provide argv or command")
        return self


def _shell_guard(args: ShellInput, ctx: ToolContext) -> ApprovalNeed | None:
    cwd = ctx.sandbox.resolve(args.cwd)
    decision = ctx.policy.check(args.argv, cwd)
    if decision.action == "deny":
        raise CommandBlockedError(f"command blocked: {decision.reason}")
    if decision.action == "approve":
        return ApprovalNeed(
            decision.category or "shell", decision.reason, {"argv": args.argv, "cwd": ctx.sandbox.relative(cwd)}
        )
    return None


async def shell_execute(args: ShellInput, ctx: ToolContext) -> dict[str, Any]:
    cwd = ctx.sandbox.resolve(args.cwd)
    if not cwd.is_dir():
        raise FileProblem(f"working directory not found: {args.cwd}")
    argv = list(args.argv)
    if os.path.basename(argv[0]) == "git":
        argv = [argv[0], *GIT_SAFE, *argv[1:]]
    result = await run_process(
        argv, timeout=args.timeout, cwd=cwd, env=scrubbed_env(ctx.sandbox.primary),
        stdin_data=args.stdin.encode("utf-8") if args.stdin is not None else None,
        raise_on_timeout=False, limits=ResourceLimits(cpu_seconds=int(args.timeout) + 10),
    )  # fmt: skip
    return {
        "argv": args.argv, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
        "duration": round(result.duration, 3), "truncated": result.truncated, "timed_out": result.returncode == -9,
    }  # fmt: skip


class PythonInput(BaseModel):
    code: str = Field(min_length=1, max_length=200_000)
    args: list[str] = Field(default_factory=list)
    timeout: float = Field(60.0, gt=0, le=1800)


def _python_guard(args: PythonInput, ctx: ToolContext) -> ApprovalNeed:
    digest = hashlib.sha256(args.code.encode("utf-8")).hexdigest()
    return ApprovalNeed(
        "code_exec", "runs supplied Python code on this machine with your user's rights",
        {"sha256": digest, "preview": args.code[:400], "args": args.args},
    )  # fmt: skip


async def python_execute(args: PythonInput, ctx: ToolContext) -> dict[str, Any]:
    scratch = ctx.sandbox.resolve(".sax_tmp", write=True)
    scratch.mkdir(parents=True, exist_ok=True)
    script = scratch / f"snippet_{hashlib.sha256(args.code.encode('utf-8')).hexdigest()[:16]}.py"
    script.write_text(args.code, encoding="utf-8")
    try:
        # -I: isolated mode (no user site-packages, no PYTHON* variables, no implicit cwd import path)
        result = await run_process(
            [sys.executable, "-I", "-B", str(script), *args.args],
            timeout=args.timeout, cwd=ctx.sandbox.primary, env=scrubbed_env(ctx.sandbox.primary),
            raise_on_timeout=False,
            limits=ResourceLimits(cpu_seconds=int(args.timeout) + 10, memory_bytes=4 * 1024**3),
        )  # fmt: skip
    finally:
        script.unlink(missing_ok=True)
    return {
        "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
        "duration": round(result.duration, 3), "timed_out": result.returncode == -9,
    }  # fmt: skip


# ------------------------------------------------------------------------- git
class GitRepoInput(BaseModel):
    repo: str = "."


class GitDiffInput(GitRepoInput):
    staged: bool = False
    paths: list[str] = Field(default_factory=list)
    max_bytes: int = Field(200_000, ge=1000, le=2_000_000)


class GitLogInput(GitRepoInput):
    limit: int = Field(20, ge=1, le=200)


class GitCommitInput(GitRepoInput):
    message: str = Field(min_length=1, max_length=5000)
    paths: list[str] = Field(default_factory=list)
    all: bool = False


async def _git(ctx: ToolContext, repo: str, *args: str, timeout: float = 60.0) -> Any:
    root = ctx.sandbox.resolve(repo)
    if not root.is_dir():
        raise FileProblem(f"repository directory not found: {repo}")
    return await run_process(
        ["git", *GIT_SAFE, "-C", str(root), *args], timeout=timeout, env=scrubbed_env(ctx.sandbox.primary),
        raise_on_timeout=False,
    )  # fmt: skip


def _git_paths(ctx: ToolContext, repo: str, paths: list[str]) -> list[str]:
    root = ctx.sandbox.resolve(repo)
    return [os.fspath(ctx.sandbox.resolve(root / p)) for p in paths]


async def git_status(args: GitRepoInput, ctx: ToolContext) -> dict[str, Any]:
    result = await _git(ctx, args.repo, "status", "--porcelain=v1", "--branch")
    if not result.ok:
        raise FileProblem(f"git status failed: {result.tail(300)}")
    lines = result.stdout.splitlines()
    branch = lines[0][3:] if lines and lines[0].startswith("## ") else ""
    changes = [{"status": line[:2].strip(), "path": line[3:]} for line in lines[1:] if line.strip()]
    return {"branch": branch, "clean": not changes, "changes": changes}


async def git_diff(args: GitDiffInput, ctx: ToolContext) -> dict[str, Any]:
    argv = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    if args.staged:
        argv.append("--cached")
    if args.paths:
        argv += ["--", *_git_paths(ctx, args.repo, args.paths)]
    result = await _git(ctx, args.repo, *argv)
    if not result.ok:
        raise FileProblem(f"git diff failed: {result.tail(300)}")
    return {"diff": result.stdout[: args.max_bytes], "truncated": len(result.stdout) > args.max_bytes}


async def git_log(args: GitLogInput, ctx: ToolContext) -> dict[str, Any]:
    result = await _git(ctx, args.repo, "log", f"-{args.limit}", "--date=iso-strict", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s")
    if not result.ok:
        raise FileProblem(f"git log failed: {result.tail(300)}")
    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
    return {"commits": commits}


async def git_commit(args: GitCommitInput, ctx: ToolContext) -> dict[str, Any]:
    ctx.sandbox.resolve(args.repo, write=True)
    if args.paths:
        added = await _git(ctx, args.repo, "add", "--", *_git_paths(ctx, args.repo, args.paths))
    elif args.all:
        added = await _git(ctx, args.repo, "add", "-A")
    else:
        added = None
    if added is not None and not added.ok:
        raise FileProblem(f"git add failed: {added.tail(300)}")
    result = await _git(ctx, args.repo, "commit", "-m", args.message)
    if not result.ok:
        raise FileProblem(f"git commit failed: {(result.stdout + result.stderr).strip()[-300:]}")
    head = await _git(ctx, args.repo, "rev-parse", "HEAD")
    return {"sha": head.stdout.strip(), "summary": result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""}


def register(registry: ToolRegistry) -> None:
    read, safe, system = PermissionLevel.READ_ONLY, PermissionLevel.SAFE_WRITE, PermissionLevel.SYSTEM_WRITE
    specs = [
        ToolSpec("filesystem.read", "Read a text file inside the workspace.", ReadInput, fs_read, read, RiskLevel.LOW, 30),
        ToolSpec("filesystem.list", "List files in a workspace directory.", ListInput, fs_list, read, RiskLevel.LOW, 30),
        ToolSpec("filesystem.write", "Create or overwrite a text file inside the workspace.", WriteInput, fs_write, safe, RiskLevel.MEDIUM, 30),
        ToolSpec("filesystem.mkdir", "Create a directory inside the workspace.", MkdirInput, fs_mkdir, safe, RiskLevel.LOW, 30),
        ToolSpec("filesystem.move", "Move or rename a file inside the workspace.", MoveInput, fs_move, safe, RiskLevel.MEDIUM, 60),
        ToolSpec("filesystem.delete", "Delete a file or directory (always needs approval).", DeleteInput, fs_delete, system, RiskLevel.HIGH, 60, guard=_delete_guard),
        ToolSpec("shell.execute", "Run an allowlisted program without a shell; risky commands need approval.", ShellInput, shell_execute, system, RiskLevel.HIGH, 3700, guard=_shell_guard),
        ToolSpec("python.execute", "Run a Python snippet in an isolated interpreter (needs approval).", PythonInput, python_execute, system, RiskLevel.HIGH, 1900, guard=_python_guard),
        ToolSpec("git.status", "Show branch and changed files.", GitRepoInput, git_status, read, RiskLevel.LOW, 60),
        ToolSpec("git.diff", "Show unstaged or staged changes.", GitDiffInput, git_diff, read, RiskLevel.LOW, 60),
        ToolSpec("git.log", "Show recent commits.", GitLogInput, git_log, read, RiskLevel.LOW, 60),
        ToolSpec("git.commit", "Stage paths and create a local commit (never pushes).", GitCommitInput, git_commit, safe, RiskLevel.MEDIUM, 120),
    ]  # fmt: skip
    for spec in specs:
        registry.register(spec)
