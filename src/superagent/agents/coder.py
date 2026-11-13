"""CoderAgent and DebuggerAgent.

Coding follows inspect → implement → test → debug → retest. Reading, testing and
diagnosing work without a language model; writing code needs one, and the agent
says so instead of pretending.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..errors import CodeError, DependencyMissing, FileProblem, SuperAgentError
from ..types import PermissionLevel
from ..utils import truncate_middle
from .base import AgentContext, AgentResult, BaseAgent, action

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache", ".ruff_cache",
              ".pytest_cache", ".next", "target", ".sax_tmp"}  # fmt: skip
_LANGUAGES = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
              ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".sql": "SQL",
              ".html": "HTML", ".css": "CSS", ".sh": "Shell", ".md": "Markdown"}  # fmt: skip
_KEY_FILES = {"readme.md", "pyproject.toml", "requirements.txt", "setup.py", "package.json", "makefile", "dockerfile",
              "tsconfig.json", "go.mod", "cargo.toml"}  # fmt: skip
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")


class RepoInput(BaseModel):
    repo: str = "."


class TestInput(RepoInput):
    command: list[str] | None = None
    must_pass: bool = False
    timeout: float = Field(900, gt=0, le=3600)


class ImplementInput(RepoInput):
    instruction: str = Field(min_length=3)
    files: list[str] = Field(default_factory=list)
    max_rounds: int = Field(3, ge=1, le=6)
    run_tests: bool = True


class FileEdit(BaseModel):
    path: str
    content: str


class EditPlan(BaseModel):
    summary: str
    files: list[FileEdit] = Field(min_length=1, max_length=20)


def _in_skipped_dir(name: str) -> bool:
    return any(part in _SKIP_DIRS for part in Path(name).parts)


class CoderAgent(BaseAgent):
    name = "coder"
    description = "Inspects a codebase, implements changes, runs tests and iterates until they pass."
    capabilities = ["code.inspect", "code.implement", "code.test", "code.review"]
    permissions = PermissionLevel.SYSTEM_WRITE

    # ---------------------------------------------------------------- inspect
    @action("inspect", description="Map a repository: languages, key files, test command and git state.", input=RepoInput)
    async def inspect(self, ctx: AgentContext, args: RepoInput) -> AgentResult:
        info = await self._inspect(ctx, args.repo)
        languages = ", ".join(f"{name} ({count})" for name, count in list(info["languages"].items())[:4]) or "no source files"
        return AgentResult(
            output=info, summary=f"{info['file_count']} files; {languages}; tests: {' '.join(info['test_command']) or 'none found'}",
            verify=[{"type": "schema", "required": ["root"], "types": {"file_count": "int"}}],
        )  # fmt: skip

    async def _inspect(self, ctx: AgentContext, repo: str) -> dict[str, Any]:
        listing = await ctx.tools.call("filesystem.list", path=repo, pattern="*", recursive=True, max_entries=5000)
        files = [e for e in listing["entries"] if e["type"] == "file" and not _in_skipped_dir(e["name"])]
        names = {e["name"] for e in files}
        languages = Counter(_LANGUAGES[Path(n).suffix.lower()] for n in names if Path(n).suffix.lower() in _LANGUAGES)
        root = Path(listing["path"])
        test_command: list[str] = []
        if any(n.startswith("tests/") or Path(n).name.startswith("test_") for n in names) and languages.get("Python"):
            python = next((str(root / c) for c in (".venv/bin/python", "venv/bin/python") if (root / c).exists()), "python")
            test_command = [python, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
        elif "package.json" in names:
            test_command = ["npm", "test", "--silent"]
        try:
            git: dict[str, Any] = await ctx.tools.call("git.status", repo=repo)
        except SuperAgentError:
            git = {"branch": "", "clean": True, "changes": [], "note": "not a git repository"}
        return {
            "root": str(root), "file_count": len(files), "languages": dict(languages.most_common()),
            "key_files": sorted(n for n in names if Path(n).name.lower() in _KEY_FILES and "/" not in n),
            "top_level": sorted({Path(n).parts[0] for n in names})[:40], "test_command": test_command, "git": git,
            "truncated": listing["truncated"],
        }  # fmt: skip

    # ------------------------------------------------------------------ tests
    @action("run_tests", description="Run the project's test suite and report pass/fail counts.", input=TestInput)
    async def run_tests(self, ctx: AgentContext, args: TestInput) -> AgentResult:
        command = args.command or (await self._inspect(ctx, args.repo))["test_command"]
        if not command:
            raise DependencyMissing("no test command could be detected", hint="pass command=[...] explicitly")
        outcome = await self._test(ctx, args.repo, command, args.timeout)
        if args.must_pass and not outcome["passed"]:
            raise CodeError(f"tests failed (exit {outcome['exit_code']})", exit_code=outcome["exit_code"], output=outcome["output_tail"])
        status = "passed" if outcome["passed"] else "FAILED"
        return AgentResult(output=outcome, summary=f"tests {status}: {outcome['counts'] or 'no summary line'}")

    async def _test(self, ctx: AgentContext, repo: str, command: list[str], timeout: float) -> dict[str, Any]:
        run = await ctx.tools.call("shell.execute", argv=command, cwd=repo, timeout=timeout)
        text = (run["stdout"] + "\n" + run["stderr"]).strip()
        counts = {kind.rstrip("s") if kind == "errors" else kind: int(n) for n, kind in _PYTEST_COUNT.findall(text[-2000:])}
        return {
            "command": command, "exit_code": run["exit_code"], "passed": run["exit_code"] == 0 and not run["timed_out"],
            "counts": counts, "timed_out": run["timed_out"], "output_tail": text[-4000:],
        }  # fmt: skip

    @action("lint", description="Run ruff over the repository and report findings.", input=RepoInput)
    async def lint(self, ctx: AgentContext, args: RepoInput) -> AgentResult:
        run = await ctx.tools.call("shell.execute", argv=["ruff", "check", ".", "--output-format", "concise"], cwd=args.repo)
        findings = [line for line in run["stdout"].splitlines() if re.match(r"^\S+:\d+:\d+:", line)]
        return AgentResult(
            output={"clean": run["exit_code"] == 0, "findings": findings[:200], "count": len(findings)},
            summary="lint clean" if run["exit_code"] == 0 else f"{len(findings)} lint findings",
        )

    # -------------------------------------------------------------- implement
    @action("implement", description="Write or change code for an instruction, then test and debug until green.",
            input=ImplementInput, needs_llm=True)  # fmt: skip
    async def implement(self, ctx: AgentContext, args: ImplementInput) -> AgentResult:
        info = await self._inspect(ctx, args.repo)
        root = Path(info["root"])
        material = await self._read_relevant(ctx, root, args, info)
        feedback = str(ctx.recovery.get("debug_context") or "")
        written: list[str] = []
        rounds: list[dict[str, Any]] = []
        tests: dict[str, Any] | None = None

        for round_number in range(1, args.max_rounds + 1):
            prompt = ctx.prompt(
                f"Repository layout: {info['top_level']}\nLanguages: {info['languages']}\n\n"
                f"Relevant files:\n{material}\n\nInstruction: {args.instruction}\n"
                + (f"\nThe previous attempt failed its tests. Fix the cause:\n{feedback}\n" if feedback else "")
                + "\nReturn every file you create or change with its complete new content. Paths are relative to the "
                "repository root. Change only what the instruction needs and keep the existing style."
            )
            plan = await ctx.ai.complete_json(prompt, EditPlan, system=ctx.system_prompt, max_tokens=16000)
            for edit in plan.files:
                target = (root / edit.path).as_posix()
                await ctx.tools.call("filesystem.write", path=target, content=edit.content)
                if target not in written:
                    written.append(target)
            await ctx.emit("coder.round", {"round": round_number, "files": [e.path for e in plan.files], "summary": plan.summary})

            if not (args.run_tests and info["test_command"]):
                rounds.append({"round": round_number, "summary": plan.summary, "tests": "not run"})
                break
            tests = await self._test(ctx, args.repo, info["test_command"], 900)
            rounds.append({"round": round_number, "summary": plan.summary, "tests": "passed" if tests["passed"] else "failed"})
            if tests["passed"]:
                break
            feedback = tests["output_tail"]
            material = await self._read_files(ctx, written, 30_000)
        else:
            raise CodeError(
                f"tests still fail after {args.max_rounds} implement/debug rounds",
                exit_code=tests["exit_code"] if tests else None, output=feedback,
            )  # fmt: skip

        warnings = [] if tests else ["no test suite was found, so the change is unverified beyond a syntax check"]
        return AgentResult(
            output={"files": written, "rounds": rounds, "tests": tests, "verified_by_tests": bool(tests and tests["passed"])},
            summary=f"changed {len(written)} file(s) in {len(rounds)} round(s); tests {'passed' if tests else 'not run'}",
            artifacts=written, warnings=warnings,
            verify=[{"type": "file_exists", "key": "files"}]
            + [{"type": "python_syntax", "path": path, "name": f"syntax:{Path(path).name}"} for path in written if path.endswith(".py")],
        )  # fmt: skip

    async def _read_relevant(self, ctx: AgentContext, root: Path, args: ImplementInput, info: dict[str, Any]) -> str:
        wanted = [(root / f).as_posix() for f in args.files]
        if not wanted:
            listing = await ctx.tools.call("filesystem.list", path=str(root), pattern="*", recursive=True, max_entries=5000)
            mentioned = {w.strip("`'\".,") for w in args.instruction.split() if "." in w}
            source = [
                e["name"] for e in listing["entries"]
                if e["type"] == "file" and not _in_skipped_dir(e["name"]) and Path(e["name"]).suffix.lower() in _LANGUAGES
                and e["size"] < 60_000
            ]  # fmt: skip
            source.sort(key=lambda n: (Path(n).name not in mentioned and n not in mentioned, n.count("/"), n))
            wanted = [(root / n).as_posix() for n in source[:12]]
        return await self._read_files(ctx, wanted, 40_000)

    @staticmethod
    async def _read_files(ctx: AgentContext, paths: list[str], budget: int) -> str:
        parts: list[str] = []
        used = 0
        for path in paths:
            try:
                content = (await ctx.tools.call("filesystem.read", path=path, max_bytes=60_000))["text"]
            except FileProblem:
                continue
            if used + len(content) > budget:
                break
            used += len(content)
            parts.append(f"--- {path}\n{content}")
        return "\n\n".join(parts) or "(no existing files)"

    # ----------------------------------------------------------------- review
    @action("review", description="Review uncommitted changes: diff statistics, lint findings and (with a model) comments.", input=RepoInput)
    async def review(self, ctx: AgentContext, args: RepoInput) -> AgentResult:
        diff = (await ctx.tools.call("git.diff", repo=args.repo))["diff"]
        added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
        files = re.findall(r"^diff --git a/(\S+)", diff, flags=re.MULTILINE)
        output: dict[str, Any] = {"files": files, "added": added, "removed": removed, "comments": ""}
        warnings: list[str] = []
        if not diff.strip():
            return AgentResult(output=output, summary="no uncommitted changes to review")
        if ctx.has_llm:
            prompt = ctx.prompt(
                "Review this diff. Report real problems only (bugs, security, broken contracts), each with file and "
                f"line. If it looks correct, say so in one line.\n\n{truncate_middle(diff, 30_000)}"
            )
            output["comments"] = (await ctx.ai.complete(prompt, system=ctx.system_prompt, max_tokens=3000)).text.strip()
        else:
            warnings.append("no language model configured: review is limited to diff statistics")
        return AgentResult(output=output, summary=f"{len(files)} file(s), +{added}/-{removed}", warnings=warnings)


# -------------------------------------------------------------------- debugger
class DiagnoseInput(BaseModel):
    log: str = ""
    command: list[str] | None = None
    cwd: str = "."


_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'"), "DEPENDENCY", "install the missing Python package '{0}' (pip install {0})"),
    (re.compile(r"ImportError: cannot import name '(\w+)' from '([\w.]+)'"), "DEPENDENCY", "'{0}' is not exported by '{1}': check the installed version of that package"),
    (re.compile(r"Cannot find module '([^']+)'"), "DEPENDENCY", "install the missing Node module '{0}' (npm install)"),
    (re.compile(r"(\S+): command not found|No such file or directory: '([^']+)'"), "DEPENDENCY", "a required program or file is missing: {0}{1}"),
    (re.compile(r"SyntaxError: (.+)"), "CODE", "fix the syntax error: {0}"),
    (re.compile(r"IndentationError: (.+)"), "CODE", "fix the indentation: {0}"),
    (re.compile(r"NameError: name '(\w+)' is not defined"), "CODE", "'{0}' is used before it is defined or imported"),
    (re.compile(r"AttributeError: (.+)"), "CODE", "attribute problem: {0}"),
    (re.compile(r"TypeError: (.+)"), "CODE", "type problem: {0}"),
    (re.compile(r"KeyError: (.+)"), "CODE", "missing key {0}: guard the lookup or check the data"),
    (re.compile(r"AssertionError(?:: (.+))?"), "CODE", "an assertion failed{0}: compare expected and actual values in the failing test"),
    (re.compile(r"PermissionError|Permission denied"), "FILE", "the process lacks permission on a file or directory"),
    (re.compile(r"FileNotFoundError: .*'([^']+)'"), "FILE", "the file '{0}' does not exist at that path"),
    (re.compile(r"Connection refused|ConnectError|Name or service not known"), "NETWORK", "the target host or service is not reachable"),
    (re.compile(r"timed out|TimeoutError"), "TIMEOUT", "the operation timed out: raise the timeout or reduce the work"),
    (re.compile(r"Invalid data found when processing input"), "MEDIA", "FFmpeg could not decode the input file: it is corrupt or not media"),
    (re.compile(r"401|Unauthorized|invalid api key", re.IGNORECASE), "AUTH", "credentials were rejected: check the API key"),
    (re.compile(r"429|rate limit", re.IGNORECASE), "RATE_LIMIT", "the provider is rate limiting: wait or use another provider"),
]  # fmt: skip
_FRAME = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\S+))?')
_PYTEST_FAIL = re.compile(r"^FAILED (\S+?)(?: - (.+))?$", re.MULTILINE)
_JS_FRAME = re.compile(r"at .*?\(?([\w./\\-]+\.[jt]sx?):(\d+):\d+\)?")


def diagnose_text(log: str) -> dict[str, Any]:
    """Rule-based failure analysis: what kind of error, where, and the obvious next step."""
    error_class, message, suggestions = "UNKNOWN", "", []
    for pattern, kind, advice in _RULES:
        match = pattern.search(log)
        if match:
            groups = [g or "" for g in match.groups()] or [""]
            if error_class == "UNKNOWN":
                error_class, message = kind, match.group(0)[:300]
            suggestion = advice.format(*groups, "", "")
            if suggestion not in suggestions:
                suggestions.append(suggestion)
    frames = _FRAME.findall(log)
    own = [f for f in frames if "site-packages" not in f[0] and "/lib/python" not in f[0]]
    location = None
    if own or frames:
        file, line, func = (own or frames)[-1]
        location = {"file": file, "line": int(line), "function": func}
    elif js := _JS_FRAME.search(log):
        location = {"file": js.group(1), "line": int(js.group(2)), "function": ""}
    failing = [{"test": test, "reason": reason} for test, reason in _PYTEST_FAIL.findall(log)][:20]
    if failing and not suggestions:
        suggestions.append("read the first failing test's assertion and compare expected with actual")
    return {
        "error_class": error_class, "message": message, "location": location, "failing_tests": failing,
        "suggestions": suggestions[:6], "excerpt": log.strip()[-1500:],
    }  # fmt: skip


class DebuggerAgent(BaseAgent):
    name = "debugger"
    description = "Classifies a failure from logs or a failing command, locates it and proposes the next step."
    capabilities = ["debug.diagnose"]
    permissions = PermissionLevel.SYSTEM_WRITE

    @action("diagnose", description="Analyse an error log (or run a command and analyse its failure).", input=DiagnoseInput)
    async def diagnose(self, ctx: AgentContext, args: DiagnoseInput) -> AgentResult:
        log = args.log
        exit_code = None
        if args.command:
            run = await ctx.tools.call("shell.execute", argv=args.command, cwd=args.cwd)
            exit_code = run["exit_code"]
            log = f"{log}\n{run['stdout']}\n{run['stderr']}".strip()
        if not log.strip():
            raise DependencyMissing("nothing to diagnose: provide a log or a command")
        diagnosis = diagnose_text(log)
        diagnosis["exit_code"] = exit_code
        if exit_code == 0 and diagnosis["error_class"] == "UNKNOWN":
            return AgentResult(output=diagnosis | {"healthy": True}, summary="the command succeeded; no error found")
        warnings: list[str] = []
        if ctx.has_llm:
            prompt = ctx.prompt(
                f"Rule-based findings: {diagnosis['error_class']} — {diagnosis['message']}\n\nLog tail:\n{diagnosis['excerpt']}\n\n"
                "Explain the root cause in two or three sentences and give the smallest concrete fix."
            )
            diagnosis["explanation"] = (await ctx.ai.complete(prompt, system=ctx.system_prompt, max_tokens=1200)).text.strip()
        else:
            warnings.append("no language model configured: diagnosis is rule-based only")
        where = f" at {diagnosis['location']['file']}:{diagnosis['location']['line']}" if diagnosis["location"] else ""
        return AgentResult(
            output=diagnosis | {"healthy": False}, warnings=warnings,
            summary=f"{diagnosis['error_class']}{where}: {diagnosis['message'][:160] or 'unrecognised failure'}",
        )  # fmt: skip
