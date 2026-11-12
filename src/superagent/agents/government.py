"""GovernmentAgent: formal Hindi office correspondence and scheme-data reports.

Drafting and analysis run automatically. Marking a document as issued is an
official act, so it always waits for a human's approval.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..data import table as tables
from ..errors import ApprovalDenied, ValidationFailed
from ..types import PermissionLevel, RiskLevel
from ..utils import slugify, truncate_middle
from .base import AgentContext, AgentResult, BaseAgent, action

LetterType = Literal["patra", "smaran_patra", "aadesh", "suchna", "prativedan"]
_INDENT = "        "
_RIGHT = " " * 48
_HEADINGS = {"aadesh": "// कार्यालय आदेश //", "suchna": "// सूचना //"}
_STATUS_HINTS = ("स्थिति", "status", "स्टेटस", "लंबित", "pending", "निराकरण")
_AREA_HINTS = ("ग्राम पंचायत", "पंचायत", "panchayat", "ग्राम", "village", "जनपद", "block", "विकासखंड", "वार्ड", "ward")
_PENDING_WORDS = ("लंबित", "pending", "open", "अपूर्ण", "शेष", "प्रक्रियाधीन", "in progress", "not done", "नहीं")
SCHEMES = {
    "sambal": "मुख्यमंत्री जनकल्याण (संबल) योजना",
    "samagra": "समग्र पोर्टल",
    "pension": "सामाजिक सुरक्षा पेंशन",
    "cm_helpline": "सी.एम. हेल्पलाइन",
    "ekyc": "ई-केवाईसी",
    "generic": "योजना",
}


class LetterInput(BaseModel):
    subject: str = Field(min_length=3)
    recipient: str = ""
    recipient_address: str = ""
    points: list[str] = Field(default_factory=list)
    body: str = ""
    letter_type: LetterType = "patra"
    reference: str = ""
    reference_no: str = ""
    date: str = ""
    sender_name: str = ""
    designation: str = ""
    office_name: str = ""
    office_address: str = ""
    copies_to: list[str] = Field(default_factory=list)
    format: Literal["docx", "md", "txt"] = "docx"
    polish: bool = True
    out_path: str | None = None


class GoswaraInput(BaseModel):
    path: str
    sheet: str | None = None
    group_by: list[str] = Field(default_factory=list)
    sum_columns: list[str] = Field(default_factory=list)
    title: str = "गोसवारा"
    out_path: str | None = None


class SchemeInput(BaseModel):
    path: str
    scheme: Literal["sambal", "samagra", "pension", "cm_helpline", "ekyc", "generic"] = "generic"
    sheet: str | None = None
    status_column: str | None = None
    area_column: str | None = None
    pending_values: list[str] = Field(default_factory=list)
    out_path: str | None = None


class IssueInput(BaseModel):
    document_path: str
    reference_no: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    recipient: str = ""
    date: str = ""


def _today() -> str:
    return datetime.now().strftime("%d/%m/%Y")


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith(("।", ".", "?", "!", ":", "-")) else text + "।"


def render_letter(args: LetterInput, *, office: str, address: str, designation: str) -> tuple[str, list[str]]:
    """Fill the standard format. Returns the text and the list of details still to be filled in."""
    missing: list[str] = []

    def need(value: str, placeholder: str) -> str:
        if value.strip():
            return value.strip()
        missing.append(placeholder)
        return f"({placeholder})"

    office = need(args.office_name or office, "कार्यालय का नाम")
    address = (args.office_address or address).strip()
    designation = need(args.designation or designation, "पदनाम")
    number = need(args.reference_no, "पत्र क्रमांक")
    date = args.date.strip() or _today()
    kind = args.letter_type
    points = [p.strip() for p in args.points if p.strip()]
    if not points and not args.body.strip():
        missing.append("पत्र की विषय-वस्तु")

    lines = [f"कार्यालय {office}"]
    if address:
        lines.append(address)
    lines += ["", f"क्रमांक : {number}{' ' * 24}दिनांक : {date}", ""]

    if kind in _HEADINGS:
        lines += [f"{' ' * 20}{_HEADINGS[kind]}", ""]
    else:
        lines += ["प्रति,", f"{_INDENT}{need(args.recipient, 'प्राप्तकर्ता का पदनाम एवं कार्यालय')}"]
        if args.recipient_address.strip():
            lines.append(f"{_INDENT}{args.recipient_address.strip()}")
        lines.append("")
        subject = args.subject.strip().rstrip("।.")
        suffix = " के संबंध में प्रतिवेदन" if kind == "prativedan" and "प्रतिवेदन" not in subject else ""
        lines.append(f"विषय :- {subject}{suffix}।")
        if args.reference.strip():
            lines.append(f"संदर्भ :- {args.reference.strip().rstrip('।.')}।")
        lines += ["", "महोदय,", ""]

    if args.body.strip():
        lines += [f"{_INDENT}{para.strip()}" for para in args.body.strip().split("\n\n")] + [""]
    elif kind == "aadesh":
        lines += [f"{_INDENT}{args.subject.strip().rstrip('।.')} के संबंध में निम्नानुसार आदेशित किया जाता है :-", ""]
        lines += [f"{i}. {_sentence(p)}" for i, p in enumerate(points, 1)]
        lines += ["", f"{_INDENT}यह आदेश तत्काल प्रभाव से लागू होगा।", ""]
    elif kind == "suchna":
        lines += [f"{_INDENT}सर्वसंबंधितों को सूचित किया जाता है कि {_sentence(points[0]) if points else ''}", ""]
        lines += [f"{i}. {_sentence(p)}" for i, p in enumerate(points[1:], 1)]
        lines += [""] if len(points) > 1 else []
    elif kind == "prativedan":
        lines += [f"{_INDENT}उपरोक्त विषय में प्रतिवेदन निम्नानुसार सादर प्रस्तुत है :-", ""]
        lines += [f"{i}. {_sentence(p)}" for i, p in enumerate(points, 1)]
        lines += ["", f"{_INDENT}प्रतिवेदन सादर प्रेषित।", ""]
    else:
        opening = (
            "उपरोक्त विषय एवं संदर्भित पत्र का अवलोकन करने का कष्ट करें। "
            if kind == "smaran_patra"
            else "उपरोक्त विषयान्तर्गत लेख है कि "
        )
        if points:
            lines += [f"{_INDENT}{opening}{_sentence(points[0])}", ""]
            for point in points[1:]:
                lines += [f"{_INDENT}{_sentence(point)}", ""]
        closing = (
            "अतः पुनः अनुरोध है कि वांछित जानकारी/कार्यवाही शीघ्र पूर्ण कर इस कार्यालय को अवगत कराने का कष्ट करें।"
            if kind == "smaran_patra"
            else "अतः अनुरोध है कि उक्त के संबंध में आवश्यक कार्यवाही करने का कष्ट करें।"
        )
        lines += [f"{_INDENT}{closing}", ""]

    if kind not in _HEADINGS:
        lines += [f"{_RIGHT}भवदीय", ""]
    signer = f"({args.sender_name.strip()})" if args.sender_name.strip() else "(हस्ताक्षर)"
    lines += ["", f"{_RIGHT}{signer}", f"{_RIGHT}{designation}", f"{_RIGHT}{office}"]

    copies = [c.strip() for c in args.copies_to if c.strip()]
    if copies:
        lines += ["", f"पृ. क्रमांक : {number}{' ' * 20}दिनांक : {date}", "प्रतिलिपि :-"]
        lines += [f"{i}. {c.rstrip('।.')} की ओर सूचनार्थ।" for i, c in enumerate(copies, 1)]
        lines += ["", f"{_RIGHT}{designation}", f"{_RIGHT}{office}"]
    return "\n".join(lines), missing


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,/\-]\d+)*", text.translate(str.maketrans("०१२३४५६७८९", "0123456789"))))


def find_column(table: tables.Table, hints: tuple[str, ...]) -> str | None:
    for hint in hints:
        for column in table.columns:
            if hint.casefold() in column.casefold():
                return column
    return None


class GovernmentAgent(BaseAgent):
    name = "government"
    description = "Drafts formal Hindi office letters, orders and notices; builds goswara and scheme pendency reports from Excel."
    capabilities = ["government.letter", "government.goswara", "government.scheme_report", "government.issue_register"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("draft_letter", description="Draft an official letter, reminder, order, notice or report in formal Hindi.", input=LetterInput)
    async def draft_letter(self, ctx: AgentContext, args: LetterInput) -> AgentResult:
        settings = ctx.settings
        text, missing = render_letter(
            args, office=settings.office_name, address=settings.office_address, designation=settings.officer_designation
        )
        warnings = [f"भरना शेष है: {item}" for item in missing]
        method = "template"
        if args.polish and ctx.has_llm:
            prompt = ctx.prompt(
                "नीचे दिए गए शासकीय पत्र के प्रारूप की भाषा को शुद्ध, औपचारिक राजभाषा हिन्दी में परिमार्जित करें। "
                "प्रारूप, क्रमांक, दिनांक, नाम, पदनाम, संख्याएँ और तथ्य बिल्कुल न बदलें और कोई नया तथ्य न जोड़ें। "
                "केवल पत्र लौटाएँ, कोई टिप्पणी नहीं।\n\n" + text
            )
            polished = (await ctx.ai.complete(prompt, system=ctx.system_prompt, max_tokens=3000)).text.strip()
            # Accept the rewrite only if it kept every number and the subject line.
            if _numbers(text) <= _numbers(polished) and ("विषय" in polished or args.letter_type in _HEADINGS):
                text, method = polished, "template+llm"
            else:
                warnings.append("भाषा-परिमार्जन अस्वीकार किया गया क्योंकि उसमें तथ्य/संख्याएँ बदल गई थीं; मूल प्रारूप रखा गया")
        target = args.out_path or str(ctx.out_dir("letters") / f"{args.letter_type}-{slugify(args.subject)}.{args.format}")
        sections = [{"text": "\n\n".join(block for block in text.split("\n\n"))}]
        written = await ctx.tools.call("document.write", path=target, title="", sections=sections, font="Noto Sans Devanagari")
        return AgentResult(
            output={"text": text, "path": written["path"], "letter_type": args.letter_type, "method": method, "missing": missing},
            summary=f"{args.letter_type} का प्रारूप तैयार: {Path(written['path']).name}", artifacts=[written["path"]],
            warnings=warnings,
            verify=[{"type": "file_exists", "key": "path", "min_bytes": 50},
                    {"type": "contains", "key": "text", "needles": ["कार्यालय", "दिनांक"]}],
        )  # fmt: skip

    @action("goswara", description="Build a goswara (abstract): counts and totals per group with a grand total.", input=GoswaraInput)
    async def goswara(self, ctx: AgentContext, args: GoswaraInput) -> AgentResult:
        source = ctx.tools.context.sandbox.resolve(args.path)
        table = await asyncio.to_thread(tables.load_table, source, sheet=args.sheet)
        aggregations: list[dict[str, str]] = [{"op": "count", "as": "संख्या"}]
        group_by = args.group_by or [c for c in [find_column(table, _AREA_HINTS)] if c]
        if not group_by:
            raise ValidationFailed("could not pick a grouping column", hint="pass group_by; columns: " + ", ".join(table.columns[:20]))
        sum_columns = [table.resolve(c) for c in args.sum_columns]
        aggregations += [{"op": "sum", "column": c, "as": f"कुल {c}"} for c in sum_columns]
        grouped = tables.group_aggregate(table, group_by, aggregations)
        grouped = tables.sort_rows(grouped, grouped.columns[0])
        total: dict[str, Any] = {grouped.columns[0]: "योग", "संख्या": sum(r["संख्या"] for r in grouped.rows)}
        for column in sum_columns:
            total[f"कुल {column}"] = sum(r[f"कुल {column}"] or 0 for r in grouped.rows)
        rows = [{"क्र.": i, **row} for i, row in enumerate(grouped.rows, 1)] + [{"क्र.": "", **total}]
        result = tables.Table(args.title, ["क्र.", *grouped.columns], rows, table.source)
        base = Path(args.out_path) if args.out_path else ctx.out_dir("goswara") / f"{slugify(args.title)}.xlsx"
        xlsx = ctx.tools.context.sandbox.resolve(base.with_suffix(".xlsx"), write=True)
        await asyncio.to_thread(tables.export_table, result, xlsx)
        docx = await ctx.tools.call(
            "document.write", path=str(xlsx.with_suffix(".docx")), title=args.title, font="Noto Sans Devanagari",
            sections=[{"table": {"columns": result.columns, "rows": result.rows}}],
        )  # fmt: skip
        return AgentResult(
            output={"path": str(xlsx), "docx_path": docx["path"], "groups": len(grouped), "total_records": total["संख्या"],
                    "source_rows": len(table), "rows": [{k: tables._jsonable(v) for k, v in r.items()} for r in rows[:60]]},
            summary=f"गोसवारा: {len(grouped)} समूह, कुल {total['संख्या']} अभिलेख", artifacts=[str(xlsx), docx["path"]],
            verify=[{"type": "file_exists", "key": "path"}, {"type": "file_exists", "key": "docx_path"},
                    {"type": "equals", "key": "total_records", "equals": len(table), "name": "grand total equals source rows"}],
        )  # fmt: skip

    @action("scheme_report", description="Pendency report for Sambal, Samagra, pension, CM Helpline or eKYC data.", input=SchemeInput)
    async def scheme_report(self, ctx: AgentContext, args: SchemeInput) -> AgentResult:
        source = ctx.tools.context.sandbox.resolve(args.path)
        table = await asyncio.to_thread(tables.load_table, source, sheet=args.sheet)
        status_column = table.resolve(args.status_column) if args.status_column else find_column(table, _STATUS_HINTS)
        if status_column is None:
            raise ValidationFailed(
                "could not find a status column", hint="pass status_column; columns: " + ", ".join(table.columns[:20])
            )
        area_column = table.resolve(args.area_column) if args.area_column else find_column(table, _AREA_HINTS)
        pending_words = tuple(w.casefold() for w in args.pending_values) or _PENDING_WORDS

        def is_pending(value: Any) -> bool:
            text = str(value or "").casefold()
            return not text.strip() or any(word in text for word in pending_words)

        total = len(table)
        pending_rows = [row for row in table.rows if is_pending(row.get(status_column))]
        by_status = tables.sort_rows(tables.group_aggregate(table, [status_column], [{"op": "count", "as": "संख्या"}]), "संख्या", True)
        area_rows: list[dict[str, Any]] = []
        if area_column:
            counts: dict[str, list[int]] = {}
            for row in table.rows:
                key = str(row.get(area_column) or "(रिक्त)").strip()
                bucket = counts.setdefault(key, [0, 0])
                bucket[0] += 1
                bucket[1] += 1 if is_pending(row.get(status_column)) else 0
            area_rows = sorted(
                ({area_column: k, "कुल": v[0], "लंबित": v[1], "निराकृत": v[0] - v[1], "लंबित %": round(v[1] * 100 / v[0], 1)}
                 for k, v in counts.items()),
                key=lambda r: (-r["लंबित"], str(r[area_column])),
            )  # fmt: skip
        scheme = SCHEMES[args.scheme]
        percent = round(len(pending_rows) * 100 / total, 1) if total else 0.0
        findings = [f"{scheme}: कुल {total} प्रकरण, जिनमें से {len(pending_rows)} लंबित ({percent}%) एवं {total - len(pending_rows)} निराकृत।"]
        if area_rows:
            worst = ", ".join(f"{r[area_column]} ({r['लंबित']})" for r in area_rows[:3] if r["लंबित"])
            if worst:
                findings.append(f"सर्वाधिक लंबित: {worst}।")
        title = f"{scheme} - लंबित प्रकरण प्रतिवेदन ({_today()})"
        sections: list[dict[str, Any]] = [
            {"heading": "सारांश", "bullets": findings},
            {"heading": "स्थिति-वार", "table": {"columns": by_status.columns, "rows": by_status.rows}},
        ]
        if area_rows:
            sections.append({"heading": f"{area_column}-वार", "table": {"columns": list(area_rows[0]), "rows": area_rows}})
        target = args.out_path or str(ctx.out_dir("scheme") / f"{args.scheme}-pendency.docx")
        written = await ctx.tools.call("document.write", path=target, title=title, sections=sections, font="Noto Sans Devanagari")
        return AgentResult(
            output={"path": written["path"], "scheme": args.scheme, "total": total, "pending": len(pending_rows),
                    "pending_percent": percent, "status_column": status_column, "area_column": area_column,
                    "by_status": by_status.head(20), "by_area": area_rows[:40], "findings": findings,
                    "text": "\n".join(findings)},
            summary=findings[0], artifacts=[written["path"]],
            verify=[{"type": "file_exists", "key": "path"}, {"type": "schema", "required": ["status_column"], "types": {"total": "int"}}],
        )  # fmt: skip

    @action("issue", description="Record a document as issued in the dispatch register (always needs approval).", input=IssueInput)
    async def issue(self, ctx: AgentContext, args: IssueInput) -> AgentResult:
        tool_ctx = ctx.tools.context
        document = tool_ctx.sandbox.resolve(args.document_path)
        if not document.is_file():
            raise ValidationFailed(f"document not found: {args.document_path}")
        if tool_ctx.approvals is None:
            raise ApprovalDenied("issuing a document needs an approval channel")
        await tool_ctx.approvals.ensure(
            action="government.issue", category="official_issue", risk=RiskLevel.HIGH,
            reason=f"mark '{truncate_middle(args.subject, 80)}' as officially issued under no. {args.reference_no}",
            details={"reference_no": args.reference_no, "document": tool_ctx.sandbox.relative(document)},
            execution_id=ctx.execution_id, task_id=ctx.task.id,
        )  # fmt: skip
        register = tool_ctx.sandbox.resolve("registers/dispatch_register.xlsx", write=True)
        entry = [args.reference_no, args.date or _today(), args.recipient, args.subject, tool_ctx.sandbox.relative(document)]
        count = await asyncio.to_thread(_append_register, register, entry)
        return AgentResult(
            output={"register": str(register), "reference_no": args.reference_no, "entries": count},
            summary=f"जावक पंजी में दर्ज: {args.reference_no}", artifacts=[str(register)],
            verify=[{"type": "file_exists", "key": "register"}],
        )  # fmt: skip


def _append_register(path: Path, entry: list[str]) -> int:
    from openpyxl import Workbook, load_workbook

    if path.exists():
        workbook = load_workbook(path)
        sheet = workbook.active
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "जावक पंजी"
        sheet.append(["क्रमांक", "दिनांक", "प्रति", "विषय", "फ़ाइल"])
    sheet.append(entry)
    workbook.save(path)
    return sheet.max_row - 1
