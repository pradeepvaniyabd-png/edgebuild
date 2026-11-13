"""FileAgent (documents) and AnalystAgent (tabular data and reports)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..data.documents import extractive_summary, keywords
from ..errors import ValidationFailed
from ..i18n import text_for
from ..types import PermissionLevel
from ..utils import slugify, truncate_middle
from .base import AgentContext, AgentResult, BaseAgent, action


class PathInput(BaseModel):
    path: str


class SummarizeInput(BaseModel):
    path: str | None = None
    text: str | None = None
    max_sentences: int = Field(6, ge=1, le=30)

    @model_validator(mode="after")
    def _one_source(self) -> SummarizeInput:
        if not self.path and not self.text:
            raise ValueError("provide path or text")
        return self


class ConvertInput(BaseModel):
    path: str
    to: Literal["md", "txt", "docx", "csv", "xlsx", "json"]
    out_path: str | None = None


class FileAgent(BaseAgent):
    name = "file"
    description = "Reads PDF, DOCX, XLSX, CSV, TXT, JSON and images; extracts text and tables, summarises and converts."
    capabilities = ["file.extract", "file.summarize", "file.convert"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("extract", description="Extract text, tables and metadata from a file.", input=PathInput)
    async def extract(self, ctx: AgentContext, args: PathInput) -> AgentResult:
        data = await ctx.tools.call("document.extract", path=args.path)
        return AgentResult(
            output=data, warnings=list(data.get("warnings", [])),
            summary=f"{data['kind']}: {data['characters']} characters" + (f", {data['pages']} pages" if data.get("pages") else ""),
            verify=[{"type": "schema", "required": ["kind"]}],
        )  # fmt: skip

    @action("summarize", description="Summarise a document or a block of text.", input=SummarizeInput)
    async def summarize(self, ctx: AgentContext, args: SummarizeInput) -> AgentResult:
        warnings: list[str] = []
        if args.path:
            data = await ctx.tools.call("document.extract", path=args.path)
            source, warnings = data["text"], list(data.get("warnings", []))
        else:
            source = args.text or ""
        if not source.strip():
            raise ValidationFailed("there is no text to summarise", hint="scanned PDFs and images need OCR, which is not available")
        if ctx.has_llm:
            prompt = ctx.prompt(
                f"Summarise the document below in at most {args.max_sentences} sentences. Keep names, numbers and dates "
                f"exact and add nothing that is not in it.\n\n{truncate_middle(source, 60_000)}"
            )
            summary = (await ctx.ai.complete(prompt, system=ctx.system_prompt, max_tokens=1500)).text.strip()
            method = "llm"
        else:
            summary = extractive_summary(source, args.max_sentences)
            method = "extractive"
            warnings.append("no language model configured: the summary is made of sentences selected from the document")
        return AgentResult(
            output={"summary": summary, "method": method, "keywords": keywords(source, 10), "source_characters": len(source)},
            summary=truncate_middle(summary, 400), warnings=warnings, verify=[{"type": "non_empty", "key": "summary"}],
        )  # fmt: skip

    @action("convert", description="Convert a document or table to md, txt, docx, csv, xlsx or json.", input=ConvertInput)
    async def convert(self, ctx: AgentContext, args: ConvertInput) -> AgentResult:
        source = Path(args.path)
        target = args.out_path or str(ctx.out_dir("converted") / f"{slugify(source.stem)}.{args.to}")
        if args.to in {"csv", "xlsx", "json"}:
            result = await ctx.tools.call("spreadsheet.analyze", path=args.path, export_path=target, preview_rows=0)
            written = result["export_path"]
        else:
            data = await ctx.tools.call("document.extract", path=args.path)
            sections = [{"text": data["text"]}]
            written = (await ctx.tools.call("document.write", path=target, title=source.stem, sections=sections))["path"]
        return AgentResult(
            output={"path": written, "format": args.to}, summary=f"converted to {Path(written).name}", artifacts=[written],
            verify=[{"type": "file_exists", "key": "path", "min_bytes": 10}],
        )  # fmt: skip


# --------------------------------------------------------------------- analyst
class AnalyzeInput(BaseModel):
    path: str
    sheet: str | None = None
    duplicate_keys: list[str] | None = None
    question: str = ""


class QueryInput(BaseModel):
    path: str
    sheet: str | None = None
    filters: list[dict[str, Any]] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    aggregations: list[dict[str, Any]] = Field(default_factory=list)
    sort_by: str | None = None
    descending: bool = False
    export_path: str | None = None


class ReportInput(BaseModel):
    analysis: dict[str, Any]
    title: str = ""
    format: Literal["md", "docx"] = "md"
    out_path: str | None = None


def build_findings(profile: dict[str, Any], duplicates: dict[str, Any] | None, language: str) -> list[str]:
    """Deterministic observations a reader would want first. Numbers come straight from the data."""
    findings = [text_for(language, "rows_cols", table=profile["table"], rows=profile["rows"], cols=profile["column_count"])]
    if duplicates is not None:
        if duplicates["duplicate_rows"]:
            findings.append(text_for(language, "duplicates", count=duplicates["duplicate_rows"], keys=", ".join(duplicates["keys"][:3])))
        else:
            findings.append(text_for(language, "no_duplicates"))
    rows = max(1, profile["rows"])
    for column in profile["columns"]:
        if column["type"] == "number" and "sum" in column and column["unique"] > 1:
            findings.append(text_for(language, "numeric", name=column["name"], total=column["sum"], mean=column["mean"],
                                     low=column["min"], high=column["max"]))  # fmt: skip
        elif column["type"] == "text" and 2 <= column["unique"] <= 12 and column.get("top_values"):
            parts = ", ".join(f"{v['value']} {v['count']} ({v['count'] * 100 // rows}%)" for v in column["top_values"][:4])
            findings.append(text_for(language, "categories", name=column["name"], parts=parts))
        if column["nulls"] and column["nulls"] / rows >= 0.1 and column["type"] != "empty":
            findings.append(text_for(language, "blanks", name=column["name"], count=column["nulls"], pct=column["nulls"] * 100 // rows))
    return findings[:14]


class AnalystAgent(BaseAgent):
    name = "analyst"
    description = "Profiles spreadsheets, finds duplicates, filters, groups, aggregates and writes reports."
    capabilities = ["data.analyze", "data.query", "report.generate"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("analyze", description="Profile a sheet: columns, totals, categories, blanks and duplicates.", input=AnalyzeInput)
    async def analyze(self, ctx: AgentContext, args: AnalyzeInput) -> AgentResult:
        result = await ctx.tools.call(
            "spreadsheet.analyze", path=args.path, sheet=args.sheet, check_duplicates=True, duplicate_keys=args.duplicate_keys
        )
        profile, duplicates = result["profile"], result.get("duplicates")
        findings = build_findings(profile, duplicates, ctx.language)
        output: dict[str, Any] = {
            "path": result["path"], "sheet": result["sheet"], "sheets": result["sheets"], "rows": profile["rows"],
            "profile": profile, "duplicates": duplicates, "findings": findings, "preview": result["preview"][:10],
            "narrative": "",
        }  # fmt: skip
        warnings = list(profile.get("notes", []))
        if ctx.has_llm:
            prompt = ctx.prompt(
                f"Data profile:\n{truncate_middle(str(profile), 9000)}\n\nMeasured findings:\n" + "\n".join(findings)
                + (f"\n\nThe user asked: {args.question}" if args.question else "")
                + "\n\nWrite a short analysis (5-8 sentences) of what stands out. Use only these numbers."
            )
            output["narrative"] = (await ctx.ai.complete(prompt, system=ctx.system_prompt, max_tokens=1500)).text.strip()
        return AgentResult(
            output=output, summary=" ".join(findings[:3]), warnings=warnings,
            verify=[{"type": "schema", "required": ["profile", "findings"]}, {"type": "row_count", "key": "rows", "min": 0}],
        )  # fmt: skip

    @action("query", description="Filter, group, aggregate, sort and optionally export a sheet.", input=QueryInput)
    async def query(self, ctx: AgentContext, args: QueryInput) -> AgentResult:
        result = await ctx.tools.call("spreadsheet.analyze", **args.model_dump(exclude_none=True))
        output = {
            "columns": result["result_columns"], "rows": result["result_rows"], "preview": result["preview"],
            "export_path": result.get("export_path"), "filtered_rows": result.get("filtered_rows"),
        }  # fmt: skip
        verify: list[dict[str, Any]] = [{"type": "schema", "required": ["columns"]}]
        if args.export_path:
            verify.append({"type": "file_exists", "key": "export_path"})
        return AgentResult(
            output=output, summary=f"{result['result_rows']} result rows", verify=verify,
            artifacts=[result["export_path"]] if result.get("export_path") else [],
        )  # fmt: skip

    @action("report", description="Write a report (Markdown or DOCX) from an analysis.", input=ReportInput)
    async def report(self, ctx: AgentContext, args: ReportInput) -> AgentResult:
        analysis = args.analysis
        profile = analysis.get("profile") or {}
        title = args.title or text_for(ctx.language, "report_title", table=profile.get("table", "data"))
        column_rows = [
            {"column": c["name"], "type": c["type"], "filled": c["non_null"], "blank": c["nulls"], "unique": c["unique"],
             "total": c.get("sum", ""), "average": c.get("mean", "")}
            for c in profile.get("columns", [])
        ]  # fmt: skip
        sections: list[dict[str, Any]] = [{"heading": text_for(ctx.language, "h_findings"), "bullets": analysis.get("findings", [])}]
        if analysis.get("narrative"):
            sections.append({"heading": text_for(ctx.language, "h_analysis"), "text": analysis["narrative"]})
        sections.append({
            "heading": text_for(ctx.language, "h_columns"),
            "table": {"columns": ["column", "type", "filled", "blank", "unique", "total", "average"], "rows": column_rows},
        })  # fmt: skip
        duplicates = analysis.get("duplicates") or {}
        if duplicates.get("sample"):
            rows = [{"values": ", ".join(f"{k}={v}" for k, v in s["values"].items())[:120], "count": s["count"],
                     "rows": ", ".join(str(n) for n in s["row_numbers"])} for s in duplicates["sample"][:15]]  # fmt: skip
            sections.append({"heading": text_for(ctx.language, "h_duplicates"),
                             "table": {"columns": ["values", "count", "rows"], "rows": rows}})  # fmt: skip
        target = args.out_path or str(ctx.out_dir("reports") / f"{slugify(title)}.{args.format}")
        written = await ctx.tools.call("document.write", path=target, title=title, sections=sections)
        key_points = list(analysis.get("findings", []))[:8]
        return AgentResult(
            output={"path": written["path"], "title": title, "format": args.format, "key_points": key_points,
                    "text": "\n".join(f"- {point}" for point in key_points)},
            summary=f"report written: {Path(written['path']).name}", artifacts=[written["path"]],
            verify=[{"type": "file_exists", "key": "path", "min_bytes": 50}, {"type": "list_min", "key": "key_points", "min": 1}],
        )  # fmt: skip
