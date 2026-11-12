from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from superagent.api.app import create_app
from superagent.config import Settings
from superagent.core.orchestrator import Platform
from superagent.doctor import run_doctor, summarize
from superagent.errors import ConfigError, ValidationFailed
from superagent.scheduler.service import SchedulerService, build_trigger, next_run_preview


@pytest.fixture
async def client(platform: Platform):
    app = create_app(platform)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            yield c, platform


async def wait_done(c: httpx.AsyncClient, execution_id: str, timeout: float = 30) -> dict:
    for _ in range(int(timeout / 0.1)):
        data = (await c.get(f"/api/v1/tasks/{execution_id}")).json()
        if not data["running"] and data["status"] not in {"PLANNING", "RUNNING"}:
            return data
        await asyncio.sleep(0.1)
    raise AssertionError("execution did not finish")


async def test_health_ready_and_inventory(client):
    c, _ = client
    assert (await c.get("/health")).json()["status"] == "ok"
    assert (await c.get("/ready")).json()["database"] == "ok"
    agents = (await c.get("/api/v1/agents")).json()
    assert {"master", "director", "government", "trading"} <= {a["name"] for a in agents}
    assert len((await c.get("/api/v1/tools")).json()) >= 20
    providers = (await c.get("/api/v1/providers")).json()
    assert {p["name"]: p["status"] for p in providers}["anthropic"] == "NOT_CONFIGURED"


async def test_task_lifecycle_events_report_and_cancel_of_unknown(client):
    c, _ = client
    created = await c.post("/api/v1/tasks", json={"command": "system status dikhao"})
    assert created.status_code == 202
    execution_id = created.json()["id"]
    data = await wait_done(c, execution_id)
    assert data["status"] == "SUCCESS" and data["tasks"][0]["agent"] == "system"
    events = await c.get(f"/api/v1/tasks/{execution_id}/events")
    assert events.headers["content-type"].startswith("text/event-stream")
    types = [line.removeprefix("event: ") for line in events.text.splitlines() if line.startswith("event: ")]
    assert types[0] == "execution.created" and types[-1] == "execution.completed" and "plan.created" in types
    assert "STATUS: COMPLETE" in (await c.get(f"/api/v1/tasks/{execution_id}/report")).json()["report"]
    assert (await c.post("/api/v1/tasks/exe_nope/cancel")).status_code == 409
    assert (await c.get("/api/v1/tasks/exe_nope")).status_code == 404
    assert (await c.post("/api/v1/tasks", json={"command": ""})).status_code == 422


async def test_approval_over_http_resumes_the_execution(client):
    c, p = client
    doc = p.settings.workspace / "letter.docx"
    from superagent.core.dag import Task
    from superagent.data.documents import write_docx

    write_docx(doc, "पत्र", [{"text": "अनुरोध है।"}])
    task = Task(description="issue", agent="government", action="issue",
                input={"document_path": "letter.docx", "reference_no": "9/2026", "subject": "विषय"})  # fmt: skip
    execution_id = await p.orchestrator.submit("issue letter", tasks=[task])
    # The server uses blocking approvals in production; here the platform parks, so the task waits for a decision.
    for _ in range(100):
        data = (await c.get(f"/api/v1/tasks/{execution_id}")).json()
        if data["approvals"] and not data["running"]:
            break
        await asyncio.sleep(0.1)
    assert data["status"] == "WAITING_APPROVAL" and data["approvals"][0]["status"] == "PENDING"
    assert len((await c.get("/api/v1/approvals")).json()) == 1
    decided = await c.post(f"/api/v1/tasks/{execution_id}/approve", json={"approve": True})
    assert decided.json()["resumed"] is True
    assert (await wait_done(c, execution_id))["status"] == "SUCCESS"
    assert (await c.post(f"/api/v1/tasks/{execution_id}/approve", json={})).status_code == 404


async def test_token_auth_rate_limit_and_loopback_guard(platform: Platform, monkeypatch):
    monkeypatch.setenv("SAX_API_TOKEN", "s3cret-token-value")
    monkeypatch.setenv("SAX_RATE_LIMIT_PER_MINUTE", "5")
    platform.settings = Settings()
    app = create_app(platform)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/health")).status_code == 200  # liveness stays open
        assert (await c.get("/api/v1/agents")).status_code == 401
        assert (await c.get("/api/v1/agents", headers={"Authorization": "Bearer wrong"})).status_code == 401
        good = {"Authorization": "Bearer s3cret-token-value"}
        assert (await c.get("/api/v1/stats", headers=good)).status_code == 200
        assert (await c.get("/api/v1/stats", headers={"X-API-Key": "s3cret-token-value"})).status_code == 200
        codes = [(await c.get("/api/v1/stats", headers=good)).status_code for _ in range(4)]
        assert codes[-1] == 429
    monkeypatch.delenv("SAX_API_TOKEN")
    monkeypatch.setenv("SAX_API_HOST", "0.0.0.0")
    with pytest.raises(ConfigError, match="non-loopback"):
        create_app(settings=Settings())


async def test_uploads_are_validated_and_files_cannot_escape_the_workspace(client):
    c, p = client
    ok = await c.post("/api/v1/uploads", files={"file": ("Sales Q3.csv", b"a,b\n1,2\n", "text/csv")})
    assert ok.status_code == 201 and ok.json()["path"].startswith("uploads/")
    assert (await c.get("/api/v1/files", params={"path": ok.json()["path"]})).content == b"a,b\n1,2\n"
    assert (await c.post("/api/v1/uploads", files={"file": ("evil.exe", b"MZ", "application/octet-stream")})).status_code == 415
    assert (await c.post("/api/v1/uploads", files={"file": ("empty.csv", b"", "text/csv")})).status_code == 400
    traversal = await c.post("/api/v1/uploads", files={"file": ("../../etc/passwd.txt", b"x", "text/plain")})
    assert traversal.status_code == 201 and ".." not in traversal.json()["path"]
    for bad in ("/etc/passwd", "../outside", "uploads/../../etc/hosts"):
        assert (await c.get("/api/v1/files", params={"path": bad})).status_code == 403
    p.settings.max_upload_mb = 1
    assert (await c.post("/api/v1/uploads", files={"file": ("big.csv", b"x" * (1024 * 1024 + 1), "text/csv")})).status_code == 413


async def test_error_bodies_never_leak_internals(client, monkeypatch):
    c, p = client

    async def boom(*_a, **_k):
        raise RuntimeError("password=hunter2hunter2 at /srv/secret/file.py")

    monkeypatch.setattr(p.repo, "counts", boom)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c._transport.app, raise_app_exceptions=False), base_url="http://t") as quiet:
        response = await quiet.get("/api/v1/stats")
    assert response.status_code == 500
    assert "hunter2" not in response.text and "/srv/" not in response.text and "Traceback" not in response.text


async def test_creative_endpoints_cover_the_project_lifecycle(client):
    c, p = client
    from tests.fakes import PaintedImages

    p.router.register(PaintedImages())
    p.settings.creative_quality = "draft"
    created = await c.post("/api/v1/creative/projects", json={"idea": "a paper boat sails home", "duration_seconds": 10, "language": "en", "style": "paper pop-up book"})
    pid = created.json()["project_id"]
    assert (await c.post(f"/api/v1/creative/projects/{pid}/generate-images")).status_code == 422  # no storyboard yet
    story = await c.post(f"/api/v1/creative/projects/{pid}/storyboard")
    assert (await wait_done(c, story.json()["id"]))["status"] == "SUCCESS"
    images = await c.post(f"/api/v1/creative/projects/{pid}/generate-images", json={"scenes": ["s01"]})
    assert (await wait_done(c, images.json()["id"]))["status"] == "SUCCESS"
    status = (await c.get(f"/api/v1/creative/projects/{pid}/status")).json()
    assert status["scenes"] == 3 and status["images"] == 1 and status["status"] == "STORYBOARDED"
    assert (await c.post(f"/api/v1/creative/projects/{pid}/render")).status_code == 422  # no clips yet
    assert (await c.post(f"/api/v1/creative/projects/{pid}/generate-images", json={"scenes": ["nope"]})).status_code == 422
    assets = (await c.get(f"/api/v1/creative/projects/{pid}/assets")).json()
    assert assets and assets[0]["kind"] == "image"
    assert (await c.get("/api/v1/creative/projects")).json()[0]["id"] == pid


# ------------------------------------------------------------------ scheduler
def test_trigger_validation_and_previews():
    for kind, expression in (("cron", "61 * * * *"), ("cron", "not cron"), ("interval", "10s"), ("interval", "abc"), ("every", "1h")):
        with pytest.raises(ValidationFailed):
            build_trigger(kind, expression, "UTC")
    with pytest.raises(ValidationFailed, match="timezone"):
        build_trigger("cron", "0 9 * * *", "Mars/Olympus")
    runs = next_run_preview("cron", "30 9 * * 1-5", "Asia/Kolkata", 3)
    assert len(runs) == 3 and all((r.hour, r.minute) == (9, 30) and r.weekday() < 5 for r in runs)


async def test_scheduled_job_persists_runs_and_tracks_failures(platform: Platform):
    scheduler = SchedulerService(platform)
    platform.scheduler = scheduler
    await scheduler.start()
    job = await scheduler.add("morning-status", "system status dikhao", trigger="cron", expression="30 9 * * *", timezone="Asia/Kolkata")
    assert job["next_run_at"] is not None and job["status"] == "active"
    with pytest.raises(ValidationFailed, match="already exists"):
        await scheduler.add("morning-status", "x", trigger="cron", expression="0 9 * * *")
    first = await scheduler.run_now(job["id"])
    row = await platform.repo.get_job(job["id"])
    assert first["status"] == "SUCCESS" and row["run_count"] == 1 and row["failure_count"] == 0 and row["last_execution_id"] == first["execution_id"]
    bad = await scheduler.add("will-fail", "missing.xlsx ka analysis karo", trigger="interval", expression="1h")
    await scheduler.run_now(bad["id"])
    await scheduler.run_now(bad["id"])
    assert (await platform.repo.get_job(bad["id"]))["failure_count"] == 2
    await scheduler.pause(bad["id"])
    assert (await platform.repo.get_job(bad["id"]))["status"] == "paused"
    await scheduler.stop()

    revived = SchedulerService(platform)  # a restart reloads active jobs from the database
    await revived.start()
    assert revived._scheduler.get_job(job["id"]) is not None and revived._scheduler.get_job(bad["id"]) is None
    assert await revived.remove(job["id"]) and not await revived.remove(job["id"])
    await revived.stop()


# ---------------------------------------------------------------------- doctor
async def test_doctor_reports_pass_warn_fail_with_remediation(platform: Platform, monkeypatch):
    platform.scheduler = SchedulerService(platform)
    await platform.scheduler.start()
    checks = {c.name: c for c in await run_doctor(platform, frontend_dir=platform.settings.home_path / "no-ui")}
    await platform.scheduler.stop()
    assert checks["database"].status == "PASS" and checks["permissions"].status == "PASS" and checks["api"].status == "PASS"
    assert checks["providers:llm"].status == "WARN" and "OLLAMA_BASE_URL" in checks["providers:llm"].fix
    assert checks["frontend_build"].status == "WARN"
    assert summarize(list(checks.values()))["FAIL"] == 0

    import shutil as sh

    real_which = sh.which
    monkeypatch.setattr("superagent.doctor.which", lambda name: None if name == "ffmpeg" else real_which(name))
    broken = {c.name: c for c in await run_doctor(platform)}
    assert broken["ffmpeg"].status == "FAIL" and "apt install ffmpeg" in broken["ffmpeg"].fix
    json.dumps([c.to_dict() for c in broken.values()])
