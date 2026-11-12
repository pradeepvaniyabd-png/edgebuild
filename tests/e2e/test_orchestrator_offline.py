from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import Workbook, load_workbook

from superagent.core.orchestrator import Platform, format_result
from superagent.types import ExecutionStatus
from tests.fakes import synthetic_candles


def write_pension_sheet(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "पेंशन"
    sheet.append(["हितग्राही", "ग्राम पंचायत", "राशि", "स्थिति"])
    for row in [("रमेश", "खेड़ा", 600, "स्वीकृत"), ("सीता", "खेड़ा", 600, "लंबित"), ("मोहन", "नयापुरा", 600, "लंबित"),
                ("गीता", "नयापुरा", 600, "स्वीकृत"), ("अनिल", "खेड़ा", 600, "लंबित"), ("रमेश", "खेड़ा", 600, "स्वीकृत")]:  # fmt: skip
        sheet.append(list(row))
    workbook.save(path)


async def test_spreadsheet_command_plans_parallel_work_and_produces_verified_files(platform: Platform):
    write_pension_sheet(platform.settings.workspace / "pension.xlsx")
    result = await platform.orchestrator.run("pension.xlsx ka analysis karo aur report banao")
    assert result.status is ExecutionStatus.SUCCESS and result.final_ready, format_result(result)
    assert result.intent.language == "hinglish" and {"DATA", "REPORT"} <= {i.value for i in result.intent.intents}
    actions = [(t["agent"], t["action"], t["status"]) for t in result.tasks]
    assert actions[:2] == [("analyst", "analyze", "SUCCESS"), ("analyst", "report", "SUCCESS")]
    assert result.tasks[0]["verification"]["passed"] and result.tasks[1]["verification"]["passed"]
    report = next(a for a in result.artifacts if a.endswith(".md"))
    text = Path(report).read_text(encoding="utf-8")
    assert "6 rows aur 4 columns" in text + result.final_text
    assert "डुप्लिकेट" not in text and "duplicate rows" in text.lower() or "duplicate" in text.lower()
    rendered = format_result(result)
    for heading in ("STATUS:", "WHAT WAS DONE", "OUTPUTS", "QUALITY CHECK", "NEXT ACTION"):
        assert heading in rendered
    stored = await platform.repo.get_execution(result.execution_id)
    assert stored["status"] == "SUCCESS" and stored["result"]["final_ready"] is True
    events = [e["type"] for e in await platform.repo.list_events(result.execution_id)]
    assert events[0] == "execution.created" and "plan.created" in events and events[-1] == "execution.completed"


async def test_government_letter_in_formal_hindi_marks_missing_details_instead_of_inventing(platform: Platform):
    result = await platform.orchestrator.run("CEO जनपद पंचायत को पेंशन प्रकरणों के संबंध में स्मरण पत्र लिखो")
    assert result.status is ExecutionStatus.SUCCESS, format_result(result)
    letter = next(a for a in result.artifacts if a.endswith(".docx"))
    from superagent.data.documents import extract

    body = extract(Path(letter)).text
    assert "स्मरण" in body or "पुनः अनुरोध" in body and "विषय" in body and "दिनांक" in body
    assert "(पत्र क्रमांक)" in body, "the reference number is unknown and must stay a visible blank"
    assert any("भरना शेष है" in w for w in result.warnings)


async def test_trading_chart_analysis_reports_structure_and_never_trades(platform: Platform):
    rows = synthetic_candles()
    path = platform.settings.workspace / "XAUUSD_15m.csv"
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        writer.writerows(rows)
    result = await platform.orchestrator.run("XAUUSD_15m.csv ka SMC analysis karo")
    assert result.status is ExecutionStatus.SUCCESS, format_result(result)
    task = result.tasks[0]
    assert (task["agent"], task["action"]) == ("trading", "analyze") and task["verification"]["passed"]
    stored = await platform.repo.load_tasks(result.execution_id)
    report = stored[0]["output"]
    assert report["candles"] == len(rows) and report["structure"]["events"]
    assert "14:30 UTC" in report["session_candles"] and "Not financial advice" in report["disclaimer"]
    assert report["symbol"] == "XAUUSD"


async def test_failed_dependency_blocks_downstream_and_resume_recovers(platform: Platform):
    result = await platform.orchestrator.run("missing.xlsx ka analysis karo aur report banao")
    assert result.status is ExecutionStatus.FAILED and not result.final_ready
    assert [t["status"] for t in result.tasks] == ["FAILED", "BLOCKED", "BLOCKED"]
    assert result.tasks[0]["failure"]["error_class"] == "FILE"
    assert "super-agent resume" in format_result(result)

    write_pension_sheet(platform.settings.workspace / "missing.xlsx")  # the user supplies the file
    resumed = await platform.orchestrator.resume(result.execution_id)
    assert resumed.status is ExecutionStatus.SUCCESS and resumed.final_ready
    assert [t["status"] for t in resumed.tasks] == ["SUCCESS", "SUCCESS", "SUCCESS"]


async def test_dispatch_register_entry_waits_for_approval_then_completes_on_resume(platform: Platform):
    doc = platform.settings.workspace / "letter.docx"
    from superagent.data.documents import write_docx

    write_docx(doc, "पत्र", [{"text": "अनुरोध है।"}])
    from superagent.core.dag import Task, TaskGraph

    task = Task(description="issue letter", agent="government", action="issue",
                input={"document_path": "letter.docx", "reference_no": "123/2026", "subject": "पेंशन"})  # fmt: skip
    execution_id = await platform.orchestrator._open("issue the letter", "test", None)
    graph = TaskGraph([task])
    await platform.repo.save_tasks(execution_id, graph.tasks)
    parked = await platform.orchestrator._drive(execution_id, "issue the letter", graph, None, [], None)
    assert parked.status is ExecutionStatus.WAITING_APPROVAL and parked.approvals
    assert not (platform.settings.workspace / "registers" / "dispatch_register.xlsx").exists()

    await platform.approvals.decide(parked.approvals[0]["id"], True, decided_by="pradeep")
    done = await platform.orchestrator.resume(execution_id)
    assert done.status is ExecutionStatus.SUCCESS
    sheet = load_workbook(platform.settings.workspace / "registers" / "dispatch_register.xlsx").active
    assert [c.value for c in sheet[2]][0] == "123/2026"


async def test_system_status_and_general_question_without_a_model(platform: Platform):
    status = await platform.orchestrator.run("system status dikhao")
    assert status.status is ExecutionStatus.SUCCESS and "provider(s) ready" in status.final_text
    asked = await platform.orchestrator.run("what is the capital of France?")
    assert asked.status is ExecutionStatus.FAILED
    assert "no llm provider available" in asked.error and "OmniRoute" in asked.error  # tells the user how to fix it


async def test_cancel_and_background_submit(platform: Platform):
    execution_id = await platform.orchestrator.submit("system status dikhao")
    result = await platform.orchestrator.wait(execution_id)
    assert result.status is ExecutionStatus.SUCCESS
    assert platform.orchestrator.cancel("exe_unknown") is False
