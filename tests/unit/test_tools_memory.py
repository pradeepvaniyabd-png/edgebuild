from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import docx
import pytest
from openpyxl import Workbook, load_workbook

from superagent.config import Settings
from superagent.core.context import ContextManager
from superagent.core.dag import Task, TaskGraph
from superagent.data import documents
from superagent.data import table as tables
from superagent.errors import (
    ApprovalRequired,
    CommandBlockedError,
    FileProblem,
    PathEscapeError,
    PermissionDeniedError,
    SecurityError,
    ToolError,
    ValidationFailed,
)
from superagent.media.probe import probe, probe_image
from superagent.memory.manager import MemoryManager, MemoryRejected
from superagent.proc import scrubbed_env
from superagent.tools import ToolContext, build_registry
from superagent.tools.web_tools import html_to_text, validate_url
from superagent.types import PermissionLevel, TaskStatus
from tests.conftest import requires_espeak, requires_ffmpeg
from tests.fakes import pdf_bytes

REGISTRY = build_registry()


async def call(ctx: ToolContext, name: str, **args):
    return await REGISTRY.call(name, args, ctx)


async def approve_pending(ctx: ToolContext, error: ApprovalRequired) -> None:
    await ctx.approvals.decide(error.approval_id, True)


# ------------------------------------------------------------------ registry
def test_every_tool_declares_schema_permission_risk_and_timeout():
    described = {tool["name"]: tool for tool in REGISTRY.describe()}
    expected = {
        "filesystem.read", "filesystem.write", "filesystem.list", "shell.execute", "python.execute", "git.status",
        "git.diff", "git.commit", "web.search", "document.extract", "spreadsheet.analyze", "browser.open",
        "image.generate", "video.generate", "audio.generate", "voice.generate", "ffmpeg.render",
    }  # fmt: skip
    assert expected <= set(described)
    for tool in described.values():
        assert tool["description"] and tool["input_schema"]["type"] == "object" and tool["timeout"] > 0
        assert tool["permission_level"] in PermissionLevel.__members__ and tool["risk_level"]


async def test_permission_level_and_argument_validation_are_enforced(tool_ctx: ToolContext):
    reader = replace(tool_ctx, permission=PermissionLevel.READ_ONLY, agent="research")
    with pytest.raises(PermissionDeniedError, match="research"):
        await call(reader, "filesystem.write", path="a.txt", content="x")
    with pytest.raises(ValidationFailed, match="max_bytes"):
        await call(tool_ctx, "filesystem.read", path="a.txt", max_bytes=-5)
    with pytest.raises(ToolError, match="unknown tool"):
        await call(tool_ctx, "teleport.now")


# ---------------------------------------------------------------- filesystem
async def test_filesystem_round_trip_stays_inside_the_workspace(tool_ctx: ToolContext, settings: Settings):
    written = await call(tool_ctx, "filesystem.write", path="notes/plan.md", content="# Plan\nstep one\n")
    assert written["created"] and Path(written["path"]).is_relative_to(settings.workspace)
    assert (await call(tool_ctx, "filesystem.read", path="notes/plan.md"))["text"].startswith("# Plan")
    with pytest.raises(FileProblem, match="already exists"):
        await call(tool_ctx, "filesystem.write", path="notes/plan.md", content="x", overwrite=False)
    await call(tool_ctx, "filesystem.move", source="notes/plan.md", destination="archive/plan.md")
    listing = await call(tool_ctx, "filesystem.list", path=".", pattern="*.md", recursive=True)
    assert [entry["name"] for entry in listing["entries"]] == ["archive/plan.md"]

    for bad in ("../../etc/passwd", "/etc/passwd", "~/.ssh/id_rsa"):
        with pytest.raises(SecurityError):
            await call(tool_ctx, "filesystem.read", path=bad)
    with pytest.raises(PathEscapeError):
        await call(tool_ctx, "filesystem.write", path="../outside.txt", content="x")
    with pytest.raises(SecurityError, match="credential-like"):
        await call(tool_ctx, "filesystem.write", path=".env", content="KEY=1")


async def test_delete_always_waits_for_approval(tool_ctx: ToolContext, settings: Settings):
    await call(tool_ctx, "filesystem.write", path="tmp/junk.txt", content="x")
    target = settings.workspace / "tmp" / "junk.txt"
    with pytest.raises(ApprovalRequired) as pending:
        await call(tool_ctx, "filesystem.delete", path="tmp/junk.txt")
    assert target.exists() and pending.value.action == "filesystem.delete"
    await approve_pending(tool_ctx, pending.value)
    await call(tool_ctx, "filesystem.delete", path="tmp/junk.txt")
    assert not target.exists()
    with pytest.raises(SecurityError, match="workspace root"):
        await call(tool_ctx, "filesystem.delete", path=".", recursive=True)


# --------------------------------------------------------------------- shell
async def test_shell_runs_safe_commands_and_gates_or_blocks_the_rest(tool_ctx: ToolContext, settings: Settings):
    await call(tool_ctx, "filesystem.write", path="a.txt", content="hello\n")
    listed = await call(tool_ctx, "shell.execute", command="ls -1")
    assert listed["exit_code"] == 0 and "a.txt" in listed["stdout"]
    # shell metacharacters are plain arguments: nothing is piped, nothing is deleted
    echoed = await call(tool_ctx, "shell.execute", command="echo hi ; rm -rf . | cat")
    assert echoed["stdout"].strip() == "hi ; rm -rf . | cat" and (settings.workspace / "a.txt").exists()

    with pytest.raises(ApprovalRequired) as pending:
        await call(tool_ctx, "shell.execute", argv=["rm", "-rf", "a.txt"])
    assert (settings.workspace / "a.txt").exists()
    for argv in (["sudo", "ls"], ["bash", "-c", "ls"], ["cat", "/etc/passwd"], ["curl-like-unknown"]):
        with pytest.raises(CommandBlockedError):
            await call(tool_ctx, "shell.execute", argv=argv)
    await approve_pending(tool_ctx, pending.value)
    assert (await call(tool_ctx, "shell.execute", argv=["rm", "-rf", "a.txt"]))["exit_code"] == 0
    assert not (settings.workspace / "a.txt").exists()


async def test_child_processes_never_see_provider_keys(tool_ctx: ToolContext, monkeypatch, repo):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-should-never-leak-000000")
    monkeypatch.setenv("SAX_AUTO_APPROVE", "code_exec")
    ctx = replace(tool_ctx, settings=Settings())
    ctx.approvals.settings = ctx.settings
    assert "ANTHROPIC_API_KEY" not in scrubbed_env(ctx.sandbox.primary)
    code = "import os, pathlib\nprint(os.environ.get('ANTHROPIC_API_KEY'))\nprint(os.environ['HOME'] == os.getcwd())"
    result = await call(ctx, "python.execute", code=code)
    assert result["exit_code"] == 0 and result["stdout"].split() == ["None", "True"]
    assert not list((ctx.sandbox.primary / ".sax_tmp").glob("*.py")), "snippet file is cleaned up"

    hung = await call(ctx, "python.execute", code="while True: pass", timeout=1)
    assert hung["timed_out"] and hung["exit_code"] != 0


async def test_python_execution_needs_approval_by_default(tool_ctx: ToolContext):
    with pytest.raises(ApprovalRequired) as pending:
        await call(tool_ctx, "python.execute", code="print(1)")
    assert pending.value.reason.startswith("runs supplied Python")


# ----------------------------------------------------------------------- git
async def test_git_tools_commit_locally_and_never_run_repo_hooks(tool_ctx: ToolContext, settings: Settings):
    repo_dir = settings.workspace / "proj"
    repo_dir.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True, env=env)
    for key, value in (("user.name", "Tester"), ("user.email", "tester@example.com")):
        subprocess.run(["git", "-C", str(repo_dir), "config", key, value], check=True, env=env)
    hook = repo_dir / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch HOOK_RAN\n")
    hook.chmod(0o755)

    await call(tool_ctx, "filesystem.write", path="proj/app.py", content="print('v1')\n")
    status = await call(tool_ctx, "git.status", repo="proj")
    assert status["changes"] == [{"status": "??", "path": "app.py"}]
    commit = await call(tool_ctx, "git.commit", repo="proj", message="feat: first", paths=["app.py"])
    assert len(commit["sha"]) == 40
    assert not (repo_dir / "HOOK_RAN").exists(), "a repository hook must not execute"
    await call(tool_ctx, "filesystem.write", path="proj/app.py", content="print('v2')\n")
    assert "+print('v2')" in (await call(tool_ctx, "git.diff", repo="proj"))["diff"]
    assert (await call(tool_ctx, "git.log", repo="proj"))["commits"][0]["subject"] == "feat: first"
    with pytest.raises(SecurityError):
        await call(tool_ctx, "git.status", repo="/etc")


# ----------------------------------------------------------------------- web
@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8000/admin", "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://10.1.2.3/",
     "http://192.168.1.1/", "file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/", "http:///nohost"],
)  # fmt: skip
async def test_ssrf_targets_are_refused(url: str):
    with pytest.raises(SecurityError):
        await validate_url(url, allow_private=False)


async def test_fetch_blocks_private_hosts_unless_opted_in(tool_ctx: ToolContext, vendors, monkeypatch):
    with pytest.raises(SecurityError, match="non-public"):
        await call(tool_ctx, "web.fetch", url=f"{vendors.base_url}/v1/models")
    monkeypatch.setenv("SAX_ALLOW_PRIVATE_NETWORK", "true")
    page = await call(replace(tool_ctx, settings=Settings()), "web.fetch", url=f"{vendors.base_url}/v1/models")
    assert page["status"] == 200 and "fake-model" in page["text"]


def test_html_is_reduced_to_readable_text():
    html = """<html><head><title> Budget  2026 </title><style>p{color:red}</style></head><body>
    <script>alert('x')</script><h1>Overview</h1><p>Total outlay is <b>12</b> crore.</p>
    <a href="/details">Read details</a><a href="javascript:void(0)">skip</a></body></html>"""
    text, title, links = html_to_text(html)
    assert title == "Budget 2026" and "alert" not in text and "color:red" not in text
    assert "Total outlay is 12 crore." in text and links == [{"href": "/details", "text": "Read details"}]


async def test_search_disabled_is_reported_not_faked(tool_ctx: ToolContext):
    from superagent.errors import DependencyMissing

    with pytest.raises(DependencyMissing, match="disabled"):
        await call(tool_ctx, "web.search", query="anything")


# ------------------------------------------------------------ documents / data
async def test_documents_are_extracted_from_pdf_docx_and_text(tool_ctx: ToolContext, settings: Settings):
    ws = settings.workspace
    (ws / "memo.pdf").write_bytes(pdf_bytes(["Quarterly review", "Revenue grew 12 percent"]))
    document = docx.Document()
    document.add_heading("Office Order", 0)
    document.add_paragraph("All staff must submit reports by Friday.")
    grid = document.add_table(rows=2, cols=2)
    grid.rows[0].cells[0].text, grid.rows[0].cells[1].text = "Name", "Block"
    grid.rows[1].cells[0].text, grid.rows[1].cells[1].text = "Asha", "North"
    document.save(str(ws / "order.docx"))
    (ws / "note.txt").write_text("नमस्ते दुनिया", encoding="utf-8")

    pdf = await call(tool_ctx, "document.extract", path="memo.pdf")
    assert pdf["kind"] == "pdf" and pdf["pages"] == 1 and "Revenue grew 12 percent" in pdf["text"]
    word = await call(tool_ctx, "document.extract", path="order.docx")
    assert "submit reports by Friday" in word["text"] and word["tables"][0]["rows"][1] == ["Asha", "North"]
    assert (await call(tool_ctx, "document.extract", path="note.txt"))["text"] == "नमस्ते दुनिया"
    with pytest.raises(FileProblem, match="unsupported"):
        (ws / "x.bin").write_bytes(b"\x00\x01")
        await call(tool_ctx, "document.extract", path="x.bin")

    written = await call(
        tool_ctx, "document.write", path="out/report.docx", title="Summary",
        sections=[{"heading": "Findings", "bullets": ["one", "two"],
                   "table": {"columns": ["k", "v"], "rows": [{"k": "rows", "v": 3}]}}],
    )  # fmt: skip
    again = await call(tool_ctx, "document.extract", path="out/report.docx")
    assert written["bytes"] > 1000 and "Findings" in again["text"] and again["tables"][0]["rows"][1] == ["rows", "3"]


def _beneficiary_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "पेंशन"
    sheet.append(["सामाजिक सुरक्षा पेंशन सूची 2026"])  # title row above the header
    sheet.append([])
    sheet.append(["समग्र आईडी", "नाम", "ग्राम पंचायत", "राशि", "स्थिति"])
    rows = [
        (101, "रमेश", "खेड़ा", "1,200", "स्वीकृत"), (102, "सीता", "खेड़ा", "600", "लंबित"),
        (103, "मोहन", "नयापुरा", "₹ 600", "स्वीकृत"), (101, "रमेश", "खेड़ा", "1,200", "स्वीकृत"),
        (104, "गीता", "नयापुरा", "६००", "स्वीकृत"), (105, "अनिल", "खेड़ा", None, "लंबित"),
    ]  # fmt: skip
    for row in rows:
        sheet.append(list(row))
    workbook.create_sheet("notes").append(["just", "a", "tiny", "sheet"])
    workbook.save(path)


async def test_spreadsheet_analysis_handles_real_office_data(tool_ctx: ToolContext, settings: Settings):
    _beneficiary_workbook(settings.workspace / "pension.xlsx")
    sheets = await call(tool_ctx, "spreadsheet.analyze", path="pension.xlsx", mode="list_sheets")
    assert [s["name"] for s in sheets["sheets"]] == ["पेंशन", "notes"]

    result = await call(
        tool_ctx, "spreadsheet.analyze", path="pension.xlsx", duplicate_keys=["समग्र आईडी"],
        filters=[{"column": "स्थिति", "op": "==", "value": "स्वीकृत"}], group_by=["ग्राम पंचायत"],
        aggregations=[{"op": "count"}, {"op": "sum", "column": "राशि", "as": "कुल राशि"}],
        sort_by="कुल राशि", descending=True, export_path="out/summary.xlsx",
    )  # fmt: skip
    profile = result["profile"]
    assert result["sheet"] == "पेंशन" and profile["rows"] == 6 and profile["header_row"] == 2
    amount = next(c for c in profile["columns"] if c["name"] == "राशि")
    assert amount["type"] == "number" and amount["sum"] == 4200 and amount["nulls"] == 1  # commas, ₹ and Hindi digits parsed
    assert result["duplicates"]["duplicate_rows"] == 1 and result["duplicates"]["sample"][0]["count"] == 2
    assert result["filtered_rows"] == 4
    assert result["preview"] == [
        {"ग्राम पंचायत": "खेड़ा", "count": 2, "कुल राशि": 2400},
        {"ग्राम पंचायत": "नयापुरा", "count": 2, "कुल राशि": 1200},
    ]
    exported = load_workbook(result["export_path"]).active
    assert [c.value for c in exported[1]] == ["ग्राम पंचायत", "count", "कुल राशि"] and exported.max_row == 3
    with pytest.raises(ValidationFailed, match="no column"):
        await call(tool_ctx, "spreadsheet.analyze", path="pension.xlsx", sort_by="does-not-exist")


def test_csv_loading_filtering_and_numeric_parsing(tmp_path: Path):
    path = tmp_path / "sales.csv"
    path.write_text("Region;Units;Revenue\nNorth;10;1,000.50\nSouth;5;(200)\nNorth;;300\n", encoding="utf-8-sig")
    loaded = tables.load_table(path)
    assert loaded.columns == ["Region", "Units", "Revenue"] and len(loaded) == 3
    assert tables.parse_number("(200)") == -200 and tables.parse_number("१२३") == 123 and tables.parse_number("n/a") is None
    north = tables.filter_rows(loaded, [{"column": "region", "op": "==", "value": "north"}, {"column": "Units", "op": "notnull"}])
    assert len(north) == 1
    grouped = tables.group_aggregate(loaded, ["Region"], [{"op": "sum", "column": "Revenue"}, {"op": "mean", "column": "Units"}])
    assert {row["Region"]: row["sum_Revenue"] for row in grouped.rows} == {"North": 1300.5, "South": -200}
    assert "| Region |" in tables.to_markdown(grouped)


def test_extractive_summary_selects_but_never_invents():
    text = ("The panchayat approved the road project. Funding of 12 lakh was sanctioned for the road project. "
            "Lunch was served. The road project will start in March and finish by June. Weather was pleasant. "
            "A committee will monitor the road project every month. Tea was good.")  # fmt: skip
    summary = documents.extractive_summary(text, max_sentences=3)
    sentences = documents.split_sentences(summary)
    assert len(sentences) == 3 and all(s in text for s in sentences)
    assert "road" in documents.keywords(text, 3)


# --------------------------------------------------------------------- media
@requires_ffmpeg
async def test_image_and_video_tools_produce_verified_files(tool_ctx: ToolContext, settings: Settings):
    images = await call(tool_ctx, "image.generate", prompt="paper fox", width=360, height=640, num_images=2)
    assert images["count"] == 2 and images["provider"] == "painted-images"
    assert all(Path(p).is_relative_to(settings.workspace) for p in images["paths"])
    assert probe_image(Path(images["paths"][0])).height == 640

    clip = await call(tool_ctx, "video.generate", image=images["paths"][0], duration=1, fps=24, width=180, height=320, camera="pan-right")
    assert clip["generative"] is False and clip["notes"] and abs(clip["duration"] - 1) < 0.1
    inspected = await call(tool_ctx, "ffprobe.inspect", path=clip["path"])
    assert inspected["kind"] == "video" and inspected["video"]["width"] == 180

    rendered = await call(tool_ctx, "ffmpeg.render", args=["-i", clip["path"], "-vf", "hflip", "-an", "flipped.mp4"])
    assert rendered["exit_code"] == 0 and (await probe(settings.workspace / "flipped.mp4")).has_video
    with pytest.raises(CommandBlockedError):
        await call(tool_ctx, "ffmpeg.render", args=["-i", "/etc/passwd", "out.mp4"])
    with pytest.raises(ApprovalRequired):
        await call(tool_ctx, "ffmpeg.render", args=["-i", "https://example.com/v.mp4", "out.mp4"])


@requires_espeak
@requires_ffmpeg
async def test_voice_tool_speaks_through_the_router(tool_ctx: ToolContext):
    spoken = await call(tool_ctx, "voice.generate", text="यह एक परीक्षण है", out_path="audio/line.wav")
    assert spoken["provider"] == "espeak-ng" and spoken["voice"] == "hi" and spoken["duration"] > 0.4


# -------------------------------------------------------------------- memory
async def test_memory_recall_ranks_by_relevance(tool_ctx: ToolContext):
    memory: MemoryManager = tool_ctx.memory
    await memory.save("User prefers reports in formal Hindi with a summary table", kind="preference", importance=0.9)
    await memory.save("The sales workbook lives in workspace/data/sales.xlsx", kind="fact")
    await memory.save("ffmpeg concat failed on mismatched fps; re-encode clips to 30 fps first", kind="fix", tags=["ffmpeg"])
    hits = await memory.search("how should I write the report for the user?")
    assert hits and hits[0].kind == "preference"
    fix = await memory.search("ffmpeg concat error with different fps", kinds=["fix"])
    assert len(fix) == 1 and "re-encode" in fix[0].content
    assert await memory.search("completely unrelated quantum chromodynamics lattice") == []
    assert "preference (1)" in await memory.summarize()


async def test_memory_refuses_secrets_redacts_pii_and_deduplicates(tool_ctx: ToolContext):
    memory: MemoryManager = tool_ctx.memory
    for secret in ("my key is sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz012345", "db password: Hunter2Hunter2",
                   "token ghp_abcdefghijklmnopqrstuvwxyz0123456789"):  # fmt: skip
        with pytest.raises(MemoryRejected, match="credential"):
            await memory.save(secret)
    assert await tool_ctx.repo.list_memories() == []

    saved = await memory.save("Beneficiary Aadhaar 1234 5678 9012 was verified")
    assert "XXXX-XXXX-9012" in saved["content"] and "5678" not in saved["content"]
    first = await memory.save("Deploy on Fridays is forbidden", kind="decision")
    second = await memory.save("Deploy on Fridays is forbidden", kind="decision", importance=0.9)
    assert second["deduplicated"] and second["id"] == first["id"]
    assert len(await tool_ctx.repo.list_memories(kind="decision")) == 1

    updated = await memory.update(first["id"], content="Deploys on Friday need sign-off", importance=1.0)
    assert updated["content"] == "Deploys on Friday need sign-off"
    with pytest.raises(MemoryRejected):
        await memory.update(first["id"], content="api_key=abcdef123456")
    assert await memory.delete(first["id"]) and not await memory.delete(first["id"])


async def test_memory_tools_and_scopes(tool_ctx: ToolContext):
    await call(tool_ctx, "memory.save", content="Project Falcon uses 9:16 at 30 fps", kind="config", scope="project:falcon")
    assert (await call(tool_ctx, "memory.search", query="falcon video format"))["results"] == []  # global scope only
    scoped = await call(tool_ctx, "memory.search", query="falcon video format", scopes=["project:falcon"])
    assert scoped["results"][0]["kind"] == "config"
    with pytest.raises(MemoryRejected):
        await call(tool_ctx, "memory.save", content="password=opensesame123")


# ------------------------------------------------------------------- context
async def test_context_includes_only_what_the_task_needs(tool_ctx: ToolContext):
    memory: MemoryManager = tool_ctx.memory
    await memory.save("User prefers bullet summaries under ten lines", kind="preference", importance=0.9)
    graph = TaskGraph([
        Task(id="load", description="load the sales sheet", agent="file", action="extract"),
        Task(id="noise", description="unrelated chart", agent="analyst", action="chart"),
        Task(id="report", description="write a sales summary report", agent="analyst", action="report",
             dependencies=["load"], metadata={"recovery": {"repair_hint": "the title was missing"}}),
    ])  # fmt: skip
    graph.get("load").status, graph.get("load").output = TaskStatus.SUCCESS, {"rows": 120, "sheet": "Q3"}
    graph.get("noise").status, graph.get("noise").output = TaskStatus.SUCCESS, {"secret_sauce": "should not appear"}

    manager = ContextManager(memory)
    pack = await manager.build(
        task=graph.get("report"), graph=graph, command="Excel analyze karo aur report banao", execution_id="exe_t",
        agent_description="Analyst: turns tables into findings.", tool_summaries=["spreadsheet.analyze: profile a sheet"],
        resolved_input={"path": "sales.xlsx"}, language="hinglish",
    )  # fmt: skip
    rendered = pack.render(include_system=True)
    assert '"rows": 120' in rendered and "should not appear" not in rendered  # direct dependencies only
    assert "bullet summaries" in pack.layer("user") and "title was missing" in pack.layer("task")
    assert "Never invent" in pack.layer("system") and "hinglish" in pack.layer("user")
    assert pack.tokens <= pack.budget and pack.stats()["dropped"] == []


async def test_context_is_compressed_to_fit_its_budget(tool_ctx: ToolContext):
    graph = TaskGraph([
        Task(id="big", description="extract", agent="file", action="extract"),
        Task(id="use", description="summarise the document", agent="file", action="summarize", dependencies=["big"]),
    ])  # fmt: skip
    graph.get("big").status, graph.get("big").output = TaskStatus.SUCCESS, {"text": "lorem ipsum dolor " * 4000}
    pack = await ContextManager(None).build(
        task=graph.get("use"), graph=graph, command="summarise " + "very long request " * 300, execution_id="exe_t",
        tool_summaries=[f"tool.{i}: does thing {i}" for i in range(40)], budget_tokens=500,
    )  # fmt: skip
    assert pack.tokens <= 500
    assert pack.layer("system") and "summarise the document" in pack.layer("task"), "protected layers survive"
    assert pack.compressed or pack.dropped
