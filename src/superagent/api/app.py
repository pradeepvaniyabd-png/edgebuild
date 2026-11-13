"""FastAPI application: tasks, approvals, creative projects, providers, costs, logs, uploads, SSE events."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..core.context import summarize_value
from ..core.orchestrator import Platform, format_result
from ..creative import ops
from ..creative.models import CreativeBrief
from ..errors import (
    ConfigError,
    FileProblem,
    NoProviderAvailable,
    PathEscapeError,
    PermissionDeniedError,
    SecurityError,
    SuperAgentError,
    ValidationFailed,
)
from ..observability import get_logger, read_log_tail
from ..scheduler.service import SchedulerService
from ..security.secrets import mask
from ..types import ApprovalStatus
from ..utils import new_id, slugify

log = get_logger("api")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_UPLOAD_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".tsv", ".pdf", ".docx", ".txt", ".md", ".json", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov",
                    ".mp3", ".wav", ".srt", ".ttf", ".otf"}  # fmt: skip


class TaskRequest(BaseModel):
    command: str = Field(min_length=1, max_length=20_000)
    project_id: str | None = None


class ApproveRequest(BaseModel):
    approval_id: str | None = None
    approve: bool = True
    scope: str = "once"


class ProjectRequest(BaseModel):
    idea: str = Field(min_length=3, max_length=5000)
    title: str = ""
    language: str = "en"
    duration_seconds: float = Field(30.0, gt=2, le=600)
    aspect_ratio: str = "9:16"
    style: str = "cinematic"
    tone: str = ""
    quality: str | None = None
    subtitles: bool = True
    ambient_music: bool = False


class SceneSelection(BaseModel):
    scenes: list[str] | None = None
    force: bool = False


class ScheduleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    command: str = Field(min_length=1)
    trigger: str = "cron"
    expression: str
    timezone: str = "UTC"


class RateLimiter:
    def __init__(self, per_minute: int):
        self.limit = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> float | None:
        now = time.monotonic()
        window = self.hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.limit:
            return max(1.0, 60 - (now - window[0]))
        window.append(now)
        return None


def create_app(platform: Platform | None = None, settings: Settings | None = None) -> FastAPI:
    settings = platform.settings if platform else (settings or get_settings())
    token = settings.api_token.get_secret_value() if settings.api_token else None
    if settings.api_host not in _LOOPBACK and not token:
        raise ConfigError("refusing to listen on a non-loopback address without an API token", hint="set SAX_API_TOKEN")
    state: dict[str, Any] = {"platform": platform}

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        if state["platform"] is None:
            state["platform"] = await Platform.create(settings, blocking_approvals=True)
            state["owned"] = True
        plat: Platform = state["platform"]
        if plat.scheduler is None:
            plat.scheduler = SchedulerService(plat)
            await plat.scheduler.start()
            state["scheduler_owned"] = True
        yield
        if state.get("scheduler_owned"):
            await plat.scheduler.stop()
        if state.get("owned"):
            await plat.aclose()

    app = FastAPI(title="Super Agent X", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-API-Key", "Last-Event-ID"])  # fmt: skip
    limiter = RateLimiter(settings.rate_limit_per_minute)

    def plat() -> Platform:
        value = state["platform"]
        if value is None:
            raise HTTPException(503, "the platform is still starting")
        return value

    async def auth(request: Request) -> None:
        if token:
            supplied = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
                raise HTTPException(401, "missing or invalid API token", headers={"WWW-Authenticate": "Bearer"})
        wait = limiter.check(request.client.host if request.client else "local")
        if wait is not None:
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(int(wait))})

    guard = [Depends(auth)]

    @app.exception_handler(SuperAgentError)
    async def handle_domain_error(_: Request, exc: SuperAgentError) -> JSONResponse:
        code = 400
        if isinstance(exc, PermissionDeniedError | SecurityError | PathEscapeError):
            code = 403
        elif isinstance(exc, ValidationFailed):
            code = 422
        elif isinstance(exc, FileProblem):
            code = 404 if "not found" in exc.message or "unknown" in exc.message else 400
        elif isinstance(exc, NoProviderAvailable | ConfigError):
            code = 503
        return JSONResponse({"error": exc.__class__.__name__, "message": mask(exc.message), "hint": exc.hint}, status_code=code)

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled API error")
        return JSONResponse({"error": "InternalError", "message": "an internal error occurred; see the server log"}, status_code=500)

    # ----------------------------------------------------------- health
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": app.version}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        value = state["platform"]
        if value is None:
            return JSONResponse({"ready": False, "reason": "starting"}, status_code=503)
        try:
            await value.db.ping()
            current = await value.db.is_current()
        except Exception as exc:
            return JSONResponse({"ready": False, "reason": f"database: {type(exc).__name__}"}, status_code=503)
        return JSONResponse({"ready": current, "database": "ok" if current else "migrations pending"}, status_code=200 if current else 503)

    # ------------------------------------------------------------ tasks
    async def view(execution_id: str) -> dict[str, Any]:
        p = plat()
        row = await p.repo.get_execution(execution_id)
        if row is None:
            raise HTTPException(404, f"unknown execution '{execution_id}'")
        tasks = await p.repo.load_tasks(execution_id)
        for task in tasks:
            task["output"] = summarize_value(task["output"], 3000) if task["output"] is not None else None
        return {**row, "running": execution_id in p.orchestrator._background, "tasks": tasks,
                "approvals": await p.repo.list_approvals(execution_id=execution_id)}  # fmt: skip

    @app.post("/api/v1/tasks", status_code=202, dependencies=guard)
    async def create_task(body: TaskRequest) -> dict[str, Any]:
        execution_id = await plat().orchestrator.submit(body.command, source="api", project_id=body.project_id)
        return {"id": execution_id, "status": "PLANNING", "url": f"/api/v1/tasks/{execution_id}"}

    @app.get("/api/v1/tasks", dependencies=guard)
    async def list_tasks(limit: int = Query(50, ge=1, le=200), status: str | None = None) -> list[dict[str, Any]]:
        return await plat().repo.list_executions(limit, status)

    @app.get("/api/v1/tasks/{execution_id}", dependencies=guard)
    async def get_task(execution_id: str) -> dict[str, Any]:
        return await view(execution_id)

    @app.post("/api/v1/tasks/{execution_id}/cancel", dependencies=guard)
    async def cancel_task(execution_id: str) -> dict[str, Any]:
        if not plat().orchestrator.cancel(execution_id):
            raise HTTPException(409, "that execution is not running")
        return {"id": execution_id, "cancelling": True}

    @app.post("/api/v1/tasks/{execution_id}/resume", status_code=202, dependencies=guard)
    async def resume_task(execution_id: str) -> dict[str, Any]:
        await plat().orchestrator.resume(execution_id, wait=False)
        return {"id": execution_id, "resuming": True}

    @app.post("/api/v1/tasks/{execution_id}/approve", dependencies=guard)
    async def approve_task(execution_id: str, body: ApproveRequest) -> dict[str, Any]:
        p = plat()
        pending = [a for a in await p.repo.list_approvals(status=ApprovalStatus.PENDING.value, execution_id=execution_id)
                   if not body.approval_id or a["id"] == body.approval_id]  # fmt: skip
        if not pending:
            raise HTTPException(404, "no pending approval for that execution")
        for approval in pending:
            await p.approvals.decide(approval["id"], body.approve, decided_by="api", scope=body.scope)
        row = await p.repo.get_execution(execution_id)
        resumed = False
        if row and row["status"] == "WAITING_APPROVAL" and body.approve and execution_id not in p.orchestrator._background:
            await p.orchestrator.resume(execution_id, wait=False)
            resumed = True
        return {"decided": [a["id"] for a in pending], "approved": body.approve, "resumed": resumed}

    @app.get("/api/v1/tasks/{execution_id}/events", dependencies=guard)
    async def task_events(execution_id: str, request: Request, after: int = 0) -> StreamingResponse:
        p = plat()
        if await p.repo.get_execution(execution_id) is None:
            raise HTTPException(404, f"unknown execution '{execution_id}'")
        last = int(request.headers.get("last-event-id") or after or 0)

        async def stream():  # noqa: ANN202
            async for event in p.events.stream(execution_id, last, heartbeat=10.0):
                if await request.is_disconnected():
                    break
                if event is None:
                    yield ": ping\n\n"
                    continue
                payload = {**event, "created_at": str(event.get("created_at"))}
                yield f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/v1/tasks/{execution_id}/report", dependencies=guard)
    async def task_report(execution_id: str) -> dict[str, str]:
        from ..core.orchestrator import ExecutionResult
        from ..types import ExecutionStatus

        data = await view(execution_id)
        rows = [{"id": t["id"], "agent": t["agent"], "action": t["action"], "status": t["status"], "summary": (t["metadata"] or {}).get("summary", ""),
                 "error": t["error"], "retries": t["retry_count"], "verification": (t["metadata"] or {}).get("verification"),
                 "failure": (t["metadata"] or {}).get("failure")} for t in data["tasks"]]  # fmt: skip
        result = ExecutionResult(execution_id, ExecutionStatus(data["status"]), data["command"], tasks=rows,
                                 final_text=((data.get("result") or {}).get("final_text", "")), artifacts=(data.get("result") or {}).get("artifacts", []),
                                 final_ready=(data.get("result") or {}).get("final_ready", False))  # fmt: skip
        return {"report": format_result(result)}

    # --------------------------------------------------------- approvals
    @app.get("/api/v1/approvals", dependencies=guard)
    async def approvals(status: str | None = "PENDING") -> list[dict[str, Any]]:
        return await plat().repo.list_approvals(status=status)

    @app.post("/api/v1/approvals/{approval_id}", dependencies=guard)
    async def decide(approval_id: str, body: ApproveRequest) -> dict[str, Any]:
        p = plat()
        decided = await p.approvals.decide(approval_id, body.approve, decided_by="api", scope=body.scope)
        if decided is None:
            raise HTTPException(404, "unknown approval")
        eid = decided.get("execution_id")
        row = await p.repo.get_execution(eid) if eid else None
        if eid and row and row["status"] == "WAITING_APPROVAL" and body.approve and eid not in p.orchestrator._background:
            await p.orchestrator.resume(eid, wait=False)
        return decided

    # ------------------------------------------------------- inventory
    @app.get("/api/v1/providers", dependencies=guard)
    async def providers(probe: bool = False) -> list[dict[str, Any]]:
        p = plat()
        health = await p.router.probe_all() if probe else p.router.health()
        return [h.model_dump(mode="json") for h in health]

    @app.get("/api/v1/agents", dependencies=guard)
    async def agents() -> list[dict[str, Any]]:
        return plat().agents.describe()

    @app.get("/api/v1/tools", dependencies=guard)
    async def tools() -> list[dict[str, Any]]:
        return plat().tools.describe()

    @app.get("/api/v1/costs", dependencies=guard)
    async def costs(project_id: str | None = None) -> dict[str, Any]:
        return await plat().cost.summary(project_id)

    @app.get("/api/v1/logs", dependencies=guard)
    async def logs(limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
        return read_log_tail(settings, limit)

    @app.get("/api/v1/stats", dependencies=guard)
    async def stats() -> dict[str, Any]:
        return await plat().repo.counts()

    # ---------------------------------------------------------- schedule
    @app.get("/api/v1/schedule", dependencies=guard)
    async def schedule_list() -> list[dict[str, Any]]:
        return await plat().scheduler.list()

    @app.post("/api/v1/schedule", status_code=201, dependencies=guard)
    async def schedule_add(body: ScheduleRequest) -> dict[str, Any]:
        return await plat().scheduler.add(body.name, body.command, trigger=body.trigger, expression=body.expression, timezone=body.timezone)

    @app.delete("/api/v1/schedule/{job_id}", dependencies=guard)
    async def schedule_remove(job_id: str) -> dict[str, bool]:
        return {"removed": await plat().scheduler.remove(job_id)}

    # ---------------------------------------------------------- creative
    def store():  # noqa: ANN202
        return plat().creative_services.store

    async def project_view(project_id: str) -> dict[str, Any]:
        p = plat()
        manifest = store().load(project_id)
        generations = await p.repo.list_generations(project_id)
        return {"manifest": manifest.model_dump(mode="json"), "assets": store().listing(project_id),
                "generations": [{k: g[k] for k in ("id", "scene_id", "kind", "status", "provider", "model", "score", "selected", "error", "attempts")} for g in generations]}  # fmt: skip

    @app.get("/api/v1/creative/projects", dependencies=guard)
    async def projects() -> list[dict[str, Any]]:
        return await plat().repo.list_projects()

    @app.post("/api/v1/creative/projects", status_code=201, dependencies=guard)
    async def create_project(body: ProjectRequest) -> dict[str, Any]:
        from ..creative.story import resolve_style

        data = body.model_dump(exclude_none=True)
        data["quality"] = data.get("quality") or settings.creative_quality
        brief = CreativeBrief.model_validate(data)
        manifest = await store().create(brief)
        manifest.style = resolve_style(brief.style)[1]
        manifest.style.aspect_ratio = brief.aspect_ratio
        store().save_manifest(manifest)
        return {"project_id": manifest.project_id, "brief": brief.model_dump(mode="json")}

    @app.get("/api/v1/creative/projects/{project_id}", dependencies=guard)
    async def get_project(project_id: str) -> dict[str, Any]:
        return await project_view(project_id)

    async def start(project_id: str, label: str, tasks) -> dict[str, Any]:  # noqa: ANN001
        execution_id = await plat().orchestrator.submit(f"{label} [{project_id}]", source="api", project_id=project_id, tasks=tasks)
        return {"id": execution_id, "url": f"/api/v1/tasks/{execution_id}"}

    @app.post("/api/v1/creative/projects/{project_id}/storyboard", status_code=202, dependencies=guard)
    async def storyboard(project_id: str) -> dict[str, Any]:
        store().load(project_id)
        return await start(project_id, "storyboard", ops.storyboard_tasks(settings, project_id))

    @app.post("/api/v1/creative/projects/{project_id}/generate-images", status_code=202, dependencies=guard)
    async def gen_images(project_id: str, body: SceneSelection | None = None) -> dict[str, Any]:
        body = body or SceneSelection()
        return await start(project_id, "generate images", ops.image_tasks(settings, store().load(project_id), body.scenes, body.force))

    @app.post("/api/v1/creative/projects/{project_id}/generate-video", status_code=202, dependencies=guard)
    async def gen_video(project_id: str, body: SceneSelection | None = None) -> dict[str, Any]:
        body = body or SceneSelection()
        return await start(project_id, "generate video", ops.video_tasks(settings, store().load(project_id), body.scenes, body.force))

    @app.post("/api/v1/creative/projects/{project_id}/voice", status_code=202, dependencies=guard)
    async def gen_voice(project_id: str, body: SceneSelection | None = None) -> dict[str, Any]:
        body = body or SceneSelection()
        return await start(project_id, "generate voice", ops.voice_tasks(settings, store().load(project_id), body.scenes))

    @app.post("/api/v1/creative/projects/{project_id}/render", status_code=202, dependencies=guard)
    async def render(project_id: str) -> dict[str, Any]:
        return await start(project_id, "render", ops.render_tasks(settings, store().load(project_id)))

    @app.post("/api/v1/creative/projects/{project_id}/qc", status_code=202, dependencies=guard)
    async def project_qc(project_id: str) -> dict[str, Any]:
        store().load(project_id)
        return await start(project_id, "quality check", ops.qc_tasks(settings, project_id))

    @app.get("/api/v1/creative/projects/{project_id}/assets", dependencies=guard)
    async def assets(project_id: str) -> list[dict[str, Any]]:
        store().load(project_id)
        return await plat().repo.list_assets(project_id)

    @app.get("/api/v1/creative/projects/{project_id}/status", dependencies=guard)
    async def project_status(project_id: str) -> dict[str, Any]:
        view_ = await project_view(project_id)
        counts: dict[str, int] = {}
        for g in view_["generations"]:
            counts[g["status"]] = counts.get(g["status"], 0) + 1
        scenes = (view_["manifest"].get("story") or {}).get("scenes", [])
        return {"project_id": project_id, "status": view_["manifest"]["status"], "generations": counts, "scenes": len(scenes),
                "images": len(view_["manifest"]["selected"].get("image", {})), "videos": len(view_["manifest"]["selected"].get("video", {})),
                "outputs": list(view_["manifest"]["outputs"])}  # fmt: skip

    # -------------------------------------------------- files and uploads
    @app.get("/api/v1/files", dependencies=guard)
    async def get_file(path: str) -> FileResponse:
        resolved = plat().sandbox.resolve(path)
        if not resolved.is_file():
            raise HTTPException(404, "file not found")
        return FileResponse(resolved)

    @app.post("/api/v1/uploads", status_code=201, dependencies=guard)
    async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
        name = Path(file.filename or "upload").name
        suffix = Path(name).suffix.lower()
        if suffix not in _UPLOAD_SUFFIXES:
            raise HTTPException(415, f"file type '{suffix or 'none'}' is not accepted")
        stem = slugify(Path(name).stem, 60) or "file"
        target = settings.uploads_dir / f"{new_id('up')[-10:]}-{stem}{suffix}"
        limit = settings.max_upload_mb * 1024 * 1024
        written = 0
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(target, "wb") as handle:
                while chunk := await file.read(1 << 20):
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(413, f"file exceeds the {settings.max_upload_mb} MB limit")
                    handle.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if written == 0:
            target.unlink(missing_ok=True)
            raise HTTPException(400, "the uploaded file is empty")
        relative = str(target.relative_to(settings.workspace))
        return {"path": relative, "bytes": written, "hint": f"mention '{relative}' in your command"}

    dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if (dist / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")

    _ = (asyncio, re)  # keep imports used by type checkers across refactors
    return app
