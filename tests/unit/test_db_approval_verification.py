from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from PIL import Image

from superagent.config import Settings
from superagent.core.approval import ApprovalManager
from superagent.core.dag import Task, TaskGraph
from superagent.core.events import EventBus
from superagent.core.verification import VerificationEngine
from superagent.db.models import Base
from superagent.db.repo import Repo
from superagent.db.session import Database
from superagent.errors import ApprovalDenied, ApprovalRequired, ConfigError
from superagent.proc import run_process
from superagent.types import ApprovalStatus, TaskStatus
from superagent.utils import file_sha256, utcnow
from tests.conftest import requires_ffmpeg


# ------------------------------------------------------------------ database
async def test_migrations_produce_exactly_the_model_schema(db: Database):
    assert await db.is_current()

    def diff(sync_conn):
        context = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)

    async with db.engine.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_unmigrated_database_is_refused_without_auto_migrate(settings: Settings):
    database = Database(f"sqlite+aiosqlite:///{settings.home_path / 'fresh.db'}")
    try:
        with pytest.raises(ConfigError, match="latest migration"):
            await database.ensure_ready(auto_migrate=False)
        await database.ensure_ready(auto_migrate=True)
        assert await database.is_current()
    finally:
        await database.dispose()


async def test_execution_and_task_graph_round_trip(repo: Repo):
    await repo.create_execution(id="exe_1", command="analyze sales.xlsx", source="cli")
    graph = TaskGraph([
        Task(id="a", description="load", agent="file", action="extract", input={"path": "sales.xlsx"}),
        Task(id="b", description="report", agent="analyst", action="report", dependencies=["a"],
             metadata={"verify": [{"type": "non_empty", "key": "text"}]}),
    ])  # fmt: skip
    await repo.save_tasks("exe_1", graph.tasks)
    a = graph.get("a")
    a.status, a.output, a.completed_at = TaskStatus.SUCCESS, {"rows": 120}, utcnow()
    await repo.save_task("exe_1", a)

    restored = TaskGraph.from_rows(await repo.load_tasks("exe_1"))
    assert [t.id for t in restored] == ["a", "b"]
    assert restored.get("a").status is TaskStatus.SUCCESS and restored.get("a").output == {"rows": 120}
    assert restored.get("b").dependencies == ["a"]
    assert restored.get("b").metadata["verify"][0]["type"] == "non_empty"
    assert restored.get("a").completed_at.tzinfo is not None

    await repo.update_execution("exe_1", status="RUNNING")
    assert await repo.mark_interrupted() == 1
    assert (await repo.get_execution("exe_1"))["status"] == "INTERRUPTED"


async def test_parallel_writes_do_not_lock_the_database(repo: Repo):
    await repo.create_execution(id="exe_p", command="x")
    await asyncio.gather(*(repo.add_event("exe_p", "task.started", {"n": n}) for n in range(60)))
    events = await repo.list_events("exe_p")
    assert len(events) == 60 and [e["id"] for e in events] == sorted(e["id"] for e in events)


async def test_costs_cache_and_counts(repo: Repo):
    await repo.add_cost(provider="p", kind="llm", estimated_usd=0.10, actual_usd=0.04, project_id="prj", priced=True)
    await repo.add_cost(provider="p", kind="image", estimated_usd=0.05, project_id="prj")
    await repo.add_cost(provider="p", kind="image", estimated_usd=1.00, project_id="other")
    assert await repo.cost_sum(project_id="prj") == pytest.approx(0.09)
    assert await repo.cost_sum() == pytest.approx(1.09)

    assert await repo.cache_get("k") is None
    await repo.cache_put("k", "image", {"path": "a.png"})
    assert await repo.cache_get("k") == {"path": "a.png"}
    assert (await repo.counts())["executions"] == 0


# ----------------------------------------------------------------- approvals
def _request(manager: ApprovalManager, **over):
    args = {
        "action": "shell.execute", "category": "destructive", "reason": "rm can delete data",
        "details": {"argv": ["rm", "-rf", "build"]}, "execution_id": "exe_1", "task_id": "t1",
    }  # fmt: skip
    return manager.ensure(**{**args, **over})


async def test_risky_action_waits_for_a_human_then_proceeds(repo: Repo, settings: Settings):
    manager = ApprovalManager(repo, settings)
    with pytest.raises(ApprovalRequired) as first:
        await _request(manager)
    with pytest.raises(ApprovalRequired) as again:  # asking again does not create a second request
        await _request(manager)
    assert again.value.approval_id == first.value.approval_id
    assert len(await repo.list_approvals(status="PENDING")) == 1

    await manager.decide(first.value.approval_id, True, decided_by="pradeep")
    await _request(manager)  # same action, now approved
    with pytest.raises(ApprovalRequired):  # a different command needs its own approval
        await _request(manager, details={"argv": ["rm", "-rf", "/"]})


async def test_denial_is_final_and_execution_scope_covers_the_category(repo: Repo, settings: Settings):
    manager = ApprovalManager(repo, settings)
    with pytest.raises(ApprovalRequired) as pending:
        await _request(manager)
    await manager.decide(pending.value.approval_id, False)
    with pytest.raises(ApprovalDenied):
        await _request(manager)

    with pytest.raises(ApprovalRequired) as code:
        await _request(manager, category="code_exec", details={"argv": ["pytest"]})
    await manager.decide(code.value.approval_id, True, scope="execution")
    await _request(manager, category="code_exec", details={"argv": ["pytest", "-x"]})  # covered
    with pytest.raises(ApprovalRequired):  # but not for another execution
        await _request(manager, category="code_exec", details={"argv": ["pytest"]}, execution_id="exe_2")


async def test_standing_policy_and_interactive_prompt(repo: Repo, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAX_AUTO_APPROVE", "code_exec")
    monkeypatch.setenv("SAX_APPROVAL_MODE", "interactive")
    settings = Settings()
    asked: list[str] = []

    async def prompt(approval: dict) -> tuple[bool, str]:
        asked.append(approval["action"])
        return approval["details"]["argv"][0] != "rm", "once"

    manager = ApprovalManager(repo, settings, prompt=prompt)
    await _request(manager, category="code_exec", details={"argv": ["pytest"]})
    assert asked == []  # standing policy: no question asked
    await _request(manager, category="network", details={"argv": ["curl", "x"]})
    with pytest.raises(ApprovalDenied):
        await _request(manager)
    assert asked == ["shell.execute", "shell.execute"]


async def test_wait_unblocks_when_decided(repo: Repo, settings: Settings):
    manager = ApprovalManager(repo, settings, blocking=True)
    with pytest.raises(ApprovalRequired) as pending:
        await _request(manager)
    waiter = asyncio.create_task(manager.wait(pending.value.approval_id))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await manager.decide(pending.value.approval_id, True)
    assert await asyncio.wait_for(waiter, timeout=3) is ApprovalStatus.APPROVED


# -------------------------------------------------------------------- events
async def test_events_are_persisted_masked_and_streamed_in_order(repo: Repo):
    bus = EventBus(repo)
    await repo.create_execution(id="exe_e", command="x")
    await bus.emit("exe_e", "execution.started", {"api_key": "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx"})

    seen: list[str] = []

    async def consume() -> None:
        async for event in bus.stream("exe_e", heartbeat=0.05):
            if event is not None:
                seen.append(event["type"])

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await bus.emit("exe_e", "task.started", {}, task_id="t1")
    await bus.emit("exe_e", "execution.completed", {"status": "SUCCESS"})
    await asyncio.wait_for(consumer, timeout=3)
    assert seen == ["execution.started", "task.started", "execution.completed"]
    stored = await repo.list_events("exe_e")
    assert "AbCdEfGh" not in str(stored[0]["payload"])


# -------------------------------------------------------------- verification
async def test_structural_checks(tmp_path: Path):
    engine = VerificationEngine()
    good = tmp_path / "out.py"
    good.write_text("def f():\n    return 1\n")
    output = {"path": str(good), "rows": [1, 2, 3], "count": 3, "exit_code": 0, "title": "Report", "text": "sales up"}
    report = await engine.verify(
        [
            {"type": "schema", "required": ["title", "rows"], "types": {"count": "int", "title": "str"}},
            {"type": "file_exists", "key": "path", "min_bytes": 5},
            {"type": "checksum", "key": "path", "sha256": file_sha256(good)},
            {"type": "python_syntax", "key": "path"},
            {"type": "row_count", "key": "rows", "equals": 3},
            {"type": "exit_code", "key": "exit_code"},
            {"type": "contains", "key": "text", "needles": ["sales"]},
            {"type": "non_empty", "key": "missing", "optional": True},
        ],
        output,
    )
    assert report.passed, report.summary()
    assert sum(c.skipped for c in report.checks) == 1


async def test_failures_are_reported_and_unknown_checks_fail_closed(tmp_path: Path):
    engine = VerificationEngine()
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n")
    report = await engine.verify(
        [
            {"type": "schema", "required": ["title"]},
            {"type": "file_exists", "path": str(tmp_path / "nope.mp4")},
            {"type": "python_syntax", "path": str(bad)},
            {"type": "row_count", "key": "rows", "min": 5},
            {"type": "exit_code", "key": "exit_code"},
            {"type": "telepathy"},
            {"type": "non_empty", "key": "absent"},
        ],
        {"rows": [1], "exit_code": 2, "title": ""},
    )
    assert not report.passed
    assert len(report.failures) == 7
    assert "unknown check type" in report.summary()


async def test_image_check_validates_real_pixels(tmp_path: Path):
    path = tmp_path / "frame.png"
    Image.new("RGB", (1080, 1920), (20, 40, 60)).save(path)
    corrupt = tmp_path / "broken.png"
    corrupt.write_bytes(b"\x89PNG\r\n\x1a\nnot really a png")
    engine = VerificationEngine()
    ok = await engine.verify([{"type": "image", "path": str(path), "aspect_ratio": 9 / 16, "min_width": 720}], {})
    assert ok.passed, ok.summary()
    assert not (await engine.verify([{"type": "image", "path": str(path), "width": 1920}], {})).passed
    assert not (await engine.verify([{"type": "image", "path": str(corrupt)}], {})).passed


@requires_ffmpeg
async def test_media_check_uses_ffprobe_on_a_real_clip(tmp_path: Path):
    clip = tmp_path / "clip.mp4"
    result = await run_process(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(clip)],
        timeout=60,
    )  # fmt: skip
    assert result.ok, result.stderr
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"this is not a video" * 50)
    engine = VerificationEngine()
    good = await engine.verify(
        [{"type": "media", "path": str(clip), "kind": "video", "has_audio": True, "width": 320, "height": 240,
          "fps": 24, "duration": 1.0, "duration_tolerance": 0.2, "codec": "h264"}],
        {},
    )  # fmt: skip
    assert good.passed, good.summary()
    assert not (await engine.verify([{"type": "media", "path": str(clip), "width": 1920}], {})).passed
    assert not (await engine.verify([{"type": "media", "path": str(garbage)}], {})).passed
