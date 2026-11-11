"""Document and spreadsheet tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..data import documents
from ..data import table as tables
from ..errors import ValidationFailed
from ..types import PermissionLevel, RiskLevel
from .registry import ToolContext, ToolRegistry, ToolSpec


class ExtractInput(BaseModel):
    path: str
    max_chars: int = Field(200_000, ge=100, le=2_000_000)


async def document_extract(args: ExtractInput, ctx: ToolContext) -> dict[str, Any]:
    path = ctx.sandbox.resolve(args.path)
    result = await asyncio.to_thread(documents.extract, path, max_chars=args.max_chars)
    return result.model_dump() | {"characters": result.characters}


class WriteDocInput(BaseModel):
    path: str
    title: str = ""
    sections: list[dict[str, Any]] = Field(default_factory=list)
    font: str | None = None


async def document_write(args: WriteDocInput, ctx: ToolContext) -> dict[str, Any]:
    path = ctx.sandbox.resolve(args.path, write=True)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        await asyncio.to_thread(documents.write_docx, path, args.title, args.sections, font=args.font)
    elif suffix in {".md", ".txt"}:
        await asyncio.to_thread(documents.write_markdown, path, args.title, args.sections)
    else:
        raise ValidationFailed(f"cannot write '{suffix}' documents", hint="use .docx, .md or .txt")
    return {"path": str(path), "bytes": path.stat().st_size}


class Condition(BaseModel):
    column: str
    op: str = "=="
    value: Any = None


class Aggregation(BaseModel):
    op: str = "count"
    column: str | None = None
    alias: str | None = Field(None, alias="as")

    model_config = {"populate_by_name": True}


class SpreadsheetInput(BaseModel):
    """One pass over a sheet: optional filter → optional group/aggregate → optional sort → optional export."""

    path: str
    sheet: str | None = None
    filters: list[Condition] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    aggregations: list[Aggregation] = Field(default_factory=list)
    sort_by: str | None = None
    descending: bool = False
    duplicate_keys: list[str] | None = None
    check_duplicates: bool = False
    export_path: str | None = None
    preview_rows: int = Field(20, ge=0, le=200)
    mode: Literal["analyze", "list_sheets"] = "analyze"


def _run_spreadsheet(args: SpreadsheetInput, source: Path, export: Path | None) -> dict[str, Any]:
    all_tables = tables.load_tables(source)
    sheets = [{"name": t.name, "rows": len(t), "columns": len(t.columns), "header_row": t.header_row} for t in all_tables]
    if args.mode == "list_sheets" or not all_tables:
        return {"path": str(source), "sheets": sheets}
    table = tables.load_table(source, sheet=args.sheet) if args.sheet else max(all_tables, key=len)
    result: dict[str, Any] = {"path": str(source), "sheets": sheets, "sheet": table.name, "profile": tables.profile(table)}
    if args.check_duplicates or args.duplicate_keys:
        result["duplicates"] = tables.find_duplicates(table, args.duplicate_keys)
    current = table
    if args.filters:
        current = tables.filter_rows(current, [c.model_dump() for c in args.filters])
        result["filtered_rows"] = len(current)
    if args.group_by:
        aggregations = [{"op": a.op, "column": a.column, "as": a.alias} for a in args.aggregations]
        current = tables.group_aggregate(current, args.group_by, aggregations)  # type: ignore[arg-type]
    if args.sort_by:
        current = tables.sort_rows(current, args.sort_by, args.descending)
    result["result_columns"] = current.columns
    result["result_rows"] = len(current)
    result["preview"] = current.head(args.preview_rows)
    if export is not None:
        tables.export_table(current, export)
        result["export_path"] = str(export)
    return result


async def spreadsheet_analyze(args: SpreadsheetInput, ctx: ToolContext) -> dict[str, Any]:
    source = ctx.sandbox.resolve(args.path)
    export = ctx.sandbox.resolve(args.export_path, write=True) if args.export_path else None
    return await asyncio.to_thread(_run_spreadsheet, args, source, export)


def register(registry: ToolRegistry) -> None:
    read, safe = PermissionLevel.READ_ONLY, PermissionLevel.SAFE_WRITE
    registry.register(
        ToolSpec("document.extract", "Extract text, tables and metadata from PDF, DOCX, XLSX, CSV, TXT, JSON or image files.",
                 ExtractInput, document_extract, read, RiskLevel.LOW, 180)  # fmt: skip
    )
    registry.register(
        ToolSpec("document.write", "Write a report as DOCX or Markdown from structured sections.",
                 WriteDocInput, document_write, safe, RiskLevel.LOW, 120)  # fmt: skip
    )
    registry.register(
        ToolSpec("spreadsheet.analyze", "Profile a sheet and optionally filter, group, aggregate, sort, de-duplicate and export it.",
                 SpreadsheetInput, spreadsheet_analyze, safe, RiskLevel.LOW, 300)  # fmt: skip
    )
