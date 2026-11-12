"""`super-agent` command line."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import typer

from ..config import Settings, get_settings
from ..core.orchestrator import ExecutionResult, Platform, format_result
from ..errors import SuperAgentError
from ..types import ExecutionStatus

app = typer.Typer(help="Super Agent X: plan, delegate, verify and deliver.", no_args_is_help=True, add_completion=False)
approvals_app = typer.Typer(help="Review and decide approval requests.", no_args_is_help=True)
schedule_app = typer.Typer(help="Recurring jobs.", no_args_is_help=True)
memory_app = typer.Typer(help="Long-term memory.", no_args_is_help=True)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
for name, sub in (("approvals", approvals_app), ("schedule", schedule_app), ("memory", memory_app), ("db", db_app)):
    app.add_typer(sub, name=name)


async def _prompt(approval: dict[str, Any]) -> tuple[bool, str]:
    typer.echo(f"\nAPPROVAL NEEDED [{approval['category']}]: {approval['reason']}")
    typer.echo(f"  action: {approval['action']}  details: {json.dumps(approval['details'], ensure_ascii=False)[:300]}")
    answer = await asyncio.to_thread(typer.prompt, "approve? [y]es once / [a]ll like this in this run / [n]o", default="n")
    return answer.strip().lower() in {"y", "yes", "a", "all"}, "execution" if answer.strip().lower() in {"a", "all"} else "once"


def with_platform(work: Callable[[Platform], Awaitable[Any]], *, settings: Settings | None = None, scheduler: bool = False) -> Any:
    async def main() -> Any:
        settings_ = settings or get_settings()
        interactive = settings_.approval_mode == "interactive" and sys.stdin.isatty()
        platform = await Platform.create(settings_, approval_prompt=_prompt if interactive else None)
        try:
            if scheduler:
                from ..scheduler.service import SchedulerService

                platform.scheduler = SchedulerService(platform)
            return await work(platform)
        finally:
            await platform.aclose()

    try:
        return asyncio.run(main())
    except SuperAgentError as exc:
        typer.secho(f"error: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.hint:
            typer.echo(f"hint: {exc.hint}", err=True)
        raise typer.Exit(1) from exc


def _finish(result: ExecutionResult, as_json: bool = False) -> None:
    typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str) if as_json else format_result(result))
    if result.status not in (ExecutionStatus.SUCCESS, ExecutionStatus.WAITING_APPROVAL):
        raise typer.Exit(1)


# ------------------------------------------------------------------- core
@app.command()
def run(command: str = typer.Argument(..., help="What you want done, in English, Hindi or Hinglish."), json_out: bool = typer.Option(False, "--json")) -> None:
    """Plan and execute a command."""
    _finish(with_platform(lambda p: p.orchestrator.run(command)), json_out)


@app.command()
def resume(execution_id: str, json_out: bool = typer.Option(False, "--json")) -> None:
    """Continue an execution from its last successful task (after an approval, a fix or a restart)."""
    _finish(with_platform(lambda p: p.orchestrator.resume(execution_id)), json_out)


@app.command()
def doctor(json_out: bool = typer.Option(False, "--json")) -> None:
    """Check the installation and explain how to fix anything wrong."""
    from ..doctor import run_doctor, summarize

    async def work(p: Platform) -> list:
        from ..scheduler.service import SchedulerService

        p.scheduler = SchedulerService(p)
        await p.scheduler.start()
        return await run_doctor(p)

    checks = with_platform(work)
    totals = summarize(checks)
    if json_out:
        typer.echo(json.dumps({"summary": totals, "checks": [c.to_dict() for c in checks]}, indent=2, ensure_ascii=False))
    else:
        colors = {"PASS": typer.colors.GREEN, "WARN": typer.colors.YELLOW, "FAIL": typer.colors.RED}
        for check in checks:
            typer.echo(f"{typer.style(check.status, fg=colors[check.status]):<14} {check.name:<22} {check.detail}")
            if check.fix and check.status != "PASS":
                typer.echo(f"{'':<14} {'':<22} -> {check.fix}")
        typer.echo(f"\n{totals['PASS']} passed, {totals['WARN']} warnings, {totals['FAIL']} failed")
    if totals["FAIL"]:
        raise typer.Exit(1)


@app.command()
def audit(json_out: bool = typer.Option(False, "--json")) -> None:
    """Security audit: auth, sandbox, command policy, file permissions, leaked secrets."""
    from ..security.audit import run_audit

    findings = with_platform(lambda p: _async_value(run_audit(p)))
    if json_out:
        typer.echo(json.dumps([f.to_dict() for f in findings], indent=2, ensure_ascii=False))
    else:
        colors = {"OK": typer.colors.GREEN, "LOW": typer.colors.BLUE, "MEDIUM": typer.colors.YELLOW, "HIGH": typer.colors.RED}
        for f in findings:
            typer.echo(f"{typer.style(f.severity, fg=colors[f.severity]):<14} {f.check:<20} {f.detail}")
            if f.fix and f.severity != "OK":
                typer.echo(f"{'':<14} {'':<20} -> {f.fix}")
    if any(f.severity == "HIGH" for f in findings):
        raise typer.Exit(1)


async def _async_value(value):  # noqa: ANN001, ANN202
    return value


@app.command()
def agents() -> None:
    """List agents and the actions they offer."""
    async def work(p: Platform) -> None:
        for agent in p.agents.describe():
            typer.echo(f"{agent['name']:<14} [{agent['permissions']}] {agent['description']}")
            for name, spec in agent["actions"].items():
                typer.echo(f"    .{name:<16} {spec['description']}{'  (needs a language model)' if spec['needs_llm'] else ''}")

    with_platform(work)


@app.command()
def tools() -> None:
    """List registered tools with permission and risk levels."""
    async def work(p: Platform) -> None:
        for tool in p.tools.describe():
            typer.echo(f"{tool['name']:<22} {tool['permission_level']:<16} risk={tool['risk_level']:<8} {tool['description']}")

    with_platform(work)


@app.command()
def tasks(execution_id: str | None = typer.Argument(None), limit: int = 20) -> None:
    """List recent executions, or show one in detail."""
    async def work(p: Platform) -> None:
        if execution_id is None:
            for row in await p.repo.list_executions(limit):
                typer.echo(f"{row['id']}  {row['status']:<17} {row['created_at']:%Y-%m-%d %H:%M}  {row['command'][:70]}")
            return
        row = await p.repo.get_execution(execution_id)
        if row is None:
            typer.secho("unknown execution", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        typer.echo(f"{row['id']}  {row['status']}  {row['command']}")
        for t in await p.repo.load_tasks(execution_id):
            typer.echo(f"  {t['status']:<17} {t['agent']}.{t['action']:<14} {(t['metadata'] or {}).get('summary') or t['error'] or ''}"[:160])

    with_platform(work)


@app.command()
def providers(probe: bool = typer.Option(False, "--probe", help="Contact each configured provider.")) -> None:
    """Show every provider and whether it is usable."""
    async def work(p: Platform) -> None:
        for h in (await p.router.probe_all()) if probe else p.router.health():
            typer.echo(f"{h.kind.value:<10} {h.name:<16} {h.tier.name:<15} {h.status.value:<15} {h.detail[:80]}")

    with_platform(work)


# -------------------------------------------------------------- creative
@app.command("create-image")
def create_image(prompt: str, width: int = 1024, height: int = 1024, count: int = 1) -> None:
    """Generate images and keep the best-scoring one."""
    from ..core.dag import Task

    async def work(p: Platform) -> ExecutionResult:
        task = Task(description="generate image", agent="imager", action="create", input={"prompt": prompt, "width": width, "height": height, "count": count})
        return await p.orchestrator.run_plan(prompt, [task])

    _finish(with_platform(work))


@app.command("create-video")
def create_video(prompt: str, json_out: bool = typer.Option(False, "--json")) -> None:
    """Idea to finished video: story, scenes, images, clips, voice, subtitles, render, QC (export waits for approval)."""
    text = prompt if "video" in prompt.lower() or "reel" in prompt.lower() else f"{prompt} (make a video)"
    _finish(with_platform(lambda p: p.orchestrator.run(text)), json_out)


@app.command()
def storyboard(prompt: str, duration: float = 30.0, aspect: str = "9:16", style: str = "cinematic", language: str = "en") -> None:
    """Create a project and its storyboard only (no media is generated)."""
    from ..core.dag import Task

    async def work(p: Platform) -> ExecutionResult:
        brief = Task(description="create the project", agent="director", action="brief",
                     input={"idea": prompt, "duration_seconds": duration, "aspect_ratio": aspect, "style": style, "language": language})
        board = Task(description="write the storyboard", agent="director", action="storyboard", dependencies=[brief.id],
                     input={"project_id": f"${{{brief.id}.project_id}}", "spawn_pipeline": False})  # fmt: skip
        result = await p.orchestrator.run_plan(prompt, [brief, board])
        typer.echo(f"project: {(await p.repo.list_projects(1))[0]['id']}")
        return result

    _finish(with_platform(work))


def _project_run(label: str, build: Callable[[Platform, Any], list]) -> None:
    async def work(p: Platform) -> ExecutionResult:
        manifest_id = label_arg["project"]
        manifest = p.creative_services.store.load(manifest_id)
        return await p.orchestrator.run_plan(f"{label} [{manifest_id}]", build(p, manifest), project_id=manifest_id)

    _finish(with_platform(work))


label_arg: dict[str, str] = {}


@app.command("generate-scene")
def generate_scene(project: str, scene: str, force: bool = False) -> None:
    """Image, narration and clip for one scene."""
    from ..creative import ops

    _ = ops
    label_arg["project"] = project
    _project_run(f"generate scene {scene}", lambda p, m: _scene_plan(p, m, scene, force))


def _scene_plan(p: Platform, manifest: Any, scene: str, force: bool) -> list:
    from ..creative import ops

    image = ops.image_tasks(p.settings, manifest, [scene], force)[0]
    voice = ops.voice_tasks(p.settings, manifest, [scene])[0]
    video = ops._t(p.settings, manifest.project_id, "videographer", "scene", f"clip for {scene}",
                   {"scene_id": scene, "force": force, "image": f"${{{image.id}}}", "voice": f"${{{voice.id}}}"}, [image.id, voice.id])  # fmt: skip
    return [image, voice, video]


@app.command()
def render(project: str) -> None:
    """Assemble the final video from the project's clips."""
    from ..creative import ops

    label_arg["project"] = project
    _project_run("render", lambda p, m: ops.render_tasks(p.settings, m))


@app.command()
def qc(project: str) -> None:
    """Run the final quality gates for a rendered project."""
    from ..creative import ops

    label_arg["project"] = project
    _project_run("quality check", lambda p, m: ops.qc_tasks(p.settings, m.project_id))


@app.command("generation-status")
def generation_status(project: str) -> None:
    """Show every generation attempt of a project: provider, status, score."""
    async def work(p: Platform) -> None:
        rows = await p.repo.list_generations(project)
        if not rows:
            typer.echo("no generations yet")
        for g in rows:
            mark = "*" if g["selected"] else " "
            score = f"{g['score']:.2f}" if g["score"] is not None else "-"
            typer.echo(f"{mark} {g['scene_id'] or '-':<8} {g['kind']:<6} {g['status']:<11} {g['provider'] or '-':<16} score={score:<5} {g['error'] or ''}"[:150])

    with_platform(work)


@app.command()
def projects() -> None:
    """List creative projects."""
    async def work(p: Platform) -> None:
        for row in await p.repo.list_projects():
            typer.echo(f"{row['id']:<44} {row['status']:<12} {row['name'][:50]}")

    with_platform(work)


# ------------------------------------------------------------- approvals
@approvals_app.command("list")
def approvals_list(all_: bool = typer.Option(False, "--all")) -> None:
    async def work(p: Platform) -> None:
        for a in await p.repo.list_approvals(None if all_ else "PENDING"):
            typer.echo(f"{a['id']}  {a['status']:<9} [{a['category']}] {a['reason']}  (execution {a['execution_id']})")

    with_platform(work)


@approvals_app.command("decide")
def approvals_decide(approval_id: str, approve: bool = typer.Option(False, "--approve"), deny: bool = typer.Option(False, "--deny"),
                     execution_scope: bool = typer.Option(False, "--for-execution", help="Also cover similar actions in this execution.")) -> None:
    if approve == deny:
        typer.secho("choose exactly one of --approve or --deny", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    async def work(p: Platform) -> None:
        decided = await p.approvals.decide(approval_id, approve, decided_by="cli", scope="execution" if execution_scope else "once")
        if decided is None:
            typer.secho("unknown approval", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        typer.echo(f"{decided['status']}. Continue with: super-agent resume {decided['execution_id']}")

    with_platform(work)


# -------------------------------------------------------------- schedule
@schedule_app.command("add")
def schedule_add(name: str = typer.Option(...), command: str = typer.Option(...), cron: str | None = None, every: str | None = None,
                 timezone: str = "UTC") -> None:
    if bool(cron) == bool(every):
        typer.secho("give exactly one of --cron or --every", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    async def work(p: Platform) -> None:
        job = await p.scheduler.add(name, command, trigger="cron" if cron else "interval", expression=cron or every or "", timezone=timezone)
        typer.echo(f"scheduled {job['id']} ({name}). It runs while `super-agent serve` is up.")

    with_platform(work, scheduler=True)


@schedule_app.command("list")
def schedule_list() -> None:
    async def work(p: Platform) -> None:
        for j in await p.repo.list_jobs():
            typer.echo(f"{j['id']}  {j['status']:<7} {j['trigger']}:{j['expression']} ({j['timezone']})  runs={j['run_count']} failures={j['failure_count']}  {j['name']}: {j['command'][:60]}")

    with_platform(work)


@schedule_app.command("remove")
def schedule_remove(job_id: str) -> None:
    async def work(p: Platform) -> None:
        typer.echo("removed" if await p.scheduler.remove(job_id) else "no such job")

    with_platform(work, scheduler=True)


@schedule_app.command("run")
def schedule_run(job_id: str) -> None:
    """Run a job once, now."""
    async def work(p: Platform) -> None:
        typer.echo(json.dumps(await p.scheduler.run_now(job_id), indent=2))

    with_platform(work, scheduler=True)


# ---------------------------------------------------------------- memory
@memory_app.command("save")
def memory_save(content: str, kind: str = "fact", scope: str = "global", importance: float = 0.5) -> None:
    async def work(p: Platform) -> None:
        saved = await p.memory.save(content, kind=kind, scope=scope, importance=importance)
        typer.echo(f"saved {saved['id']}" + (" (merged with a near-duplicate)" if saved.get("deduplicated") else ""))

    with_platform(work)


@memory_app.command("search")
def memory_search(query: str, k: int = 5) -> None:
    async def work(p: Platform) -> None:
        for hit in await p.memory.search(query, k=k):
            typer.echo(f"{hit.score:.2f}  [{hit.kind}] {hit.content}")

    with_platform(work)


# --------------------------------------------------------- ops and server
@app.command()
def costs() -> None:
    """Spend so far against the budgets."""
    async def work(p: Platform) -> None:
        typer.echo(json.dumps(await p.cost.summary(), indent=2))

    with_platform(work)


@app.command()
def logs(limit: int = 40) -> None:
    from ..observability import read_log_tail

    for record in read_log_tail(get_settings(), limit):
        typer.echo(f"{record.get('ts', '')[11:19]} {record.get('level', ''):<7} {record.get('msg', '')}"[:200])


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply database migrations."""
    from ..db.session import run_migrations

    settings = get_settings()
    settings.ensure_dirs()
    run_migrations(settings.db_url)
    typer.echo("database is up to date")


@app.command()
def serve(host: str | None = None, port: int | None = None) -> None:
    """Start the API and dashboard server (also runs scheduled jobs)."""
    import uvicorn

    from ..api.app import create_app

    settings = get_settings()
    if host:
        settings.api_host = host
    if port:
        settings.api_port = port
    uvicorn.run(create_app(settings=settings), host=settings.api_host, port=settings.api_port, log_level=settings.log_level.lower())


if __name__ == "__main__":  # pragma: no cover
    app()
