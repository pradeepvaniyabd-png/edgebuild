"""Tabular data without pandas: load, profile, filter, group, sort, de-duplicate, export.

Handles the realities of office spreadsheets: title rows above the header, merged
cells, numbers stored as text with commas or currency signs, and Hindi (Devanagari)
headers, values and digits.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..errors import FileProblem, ValidationFailed

MAX_ROWS = 300_000
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_STRIP_NUMERIC = re.compile(r"[,\s₹$€£%]|rs\.?|inr", re.IGNORECASE)
AGGREGATIONS = ("count", "sum", "mean", "min", "max", "nunique")
OPERATORS = ("==", "!=", ">", ">=", "<", "<=", "contains", "startswith", "endswith", "in", "isnull", "notnull")


@dataclass
class Table:
    name: str
    columns: list[str]
    rows: list[dict[str, Any]]
    source: str = ""
    header_row: int = 1
    notes: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)

    def resolve(self, name: str) -> str:
        """Find a column by exact, case-insensitive or whitespace-insensitive name."""
        if name in self.columns:
            return name
        wanted = _key(name)
        for column in self.columns:
            if _key(column) == wanted:
                return column
        partial = [column for column in self.columns if wanted and wanted in _key(column)]
        if len(partial) == 1:
            return partial[0]
        raise ValidationFailed(f"no column '{name}' in '{self.name}'", hint="available: " + ", ".join(self.columns[:30]))

    def values(self, column: str) -> list[Any]:
        column = self.resolve(column)
        return [row.get(column) for row in self.rows]

    def head(self, count: int = 5) -> list[dict[str, Any]]:
        return [{k: _jsonable(v) for k, v in row.items()} for row in self.rows[:count]]


def _key(text: Any) -> str:
    return re.sub(r"[\s_\-./]+", "", str(text)).casefold()


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def parse_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().translate(_DEVANAGARI_DIGITS)
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = _STRIP_NUMERIC.sub("", text.strip("()"))
    if not _NUMBER.match(text):
        return None
    number = float(text)
    return -number if negative else number


# ----------------------------------------------------------------------- loading
def load_tables(path: Path, *, sheet: str | None = None) -> list[Table]:
    if not path.is_file():
        raise FileProblem(f"file not found: {path}")
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return _load_xlsx(path, sheet)
    if suffix in {".csv", ".tsv", ".txt"}:
        return [_load_csv(path)]
    if suffix == ".json":
        return [_load_json(path)]
    if suffix == ".xls":
        raise FileProblem("legacy .xls is not supported", hint="save the workbook as .xlsx and retry")
    raise FileProblem(f"unsupported table format '{suffix}'", hint="use .xlsx, .csv, .tsv or .json")


def load_table(path: Path, *, sheet: str | None = None) -> Table:
    """The requested sheet, or the sheet with the most data."""
    tables = load_tables(path, sheet=sheet)
    if not tables:
        raise FileProblem(f"no tabular data found in {path.name}")
    return max(tables, key=len)


def _load_xlsx(path: Path, sheet: str | None) -> list[Table]:
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except (InvalidFileException, OSError, KeyError, ValueError) as exc:
        raise FileProblem(f"cannot open workbook {path.name}: {exc}") from exc
    try:
        names = workbook.sheetnames
        if sheet is not None:
            match = next((n for n in names if _key(n) == _key(sheet)), None)
            if match is None:
                raise ValidationFailed(f"no sheet '{sheet}' in {path.name}", hint="sheets: " + ", ".join(names))
            names = [match]
        tables: list[Table] = []
        for name in names:
            raw: list[list[Any]] = []
            truncated = False
            for row in workbook[name].iter_rows(values_only=True):
                if len(raw) >= MAX_ROWS:
                    truncated = True
                    break
                raw.append(list(row))
            table = _from_grid(name, raw, source=f"{path.name}#{name}")
            if table is not None:
                if truncated:
                    table.notes.append(f"only the first {MAX_ROWS} rows were read")
                tables.append(table)
        return tables
    finally:
        workbook.close()


def _load_csv(path: Path) -> Table:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes any byte sequence
        raise FileProblem(f"cannot decode {path.name}")
    sample = text[:8000]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    grid = [list(row) for row in csv.reader(text.splitlines(), dialect)][:MAX_ROWS]
    table = _from_grid(path.stem, grid, source=path.name)
    if table is None:
        raise FileProblem(f"{path.name} has no rows")
    return table


def _load_json(path: Path) -> Table:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise FileProblem(f"cannot read {path.name}: {exc}") from exc
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise FileProblem(f"{path.name} is not a list of records")
    columns: list[str] = []
    for item in data:
        for key in item:
            if key not in columns:
                columns.append(str(key))
    return Table(path.stem, columns, [{c: item.get(c) for c in columns} for item in data[:MAX_ROWS]], source=path.name)


def _from_grid(name: str, grid: list[list[Any]], *, source: str) -> Table | None:
    grid = [row for row in grid if any(not _blank(cell) for cell in row)]
    if not grid:
        return None
    header_index = _detect_header(grid)
    header = grid[header_index]
    width = max(len(row) for row in grid[header_index:])
    columns: list[str] = []
    seen: Counter[str] = Counter()
    for index in range(width):
        raw = header[index] if index < len(header) else None
        label = re.sub(r"\s+", " ", str(raw)).strip() if not _blank(raw) else f"column_{index + 1}"
        seen[label] += 1
        columns.append(label if seen[label] == 1 else f"{label}_{seen[label]}")
    # drop columns that are empty in the header and in every data row
    keep = [
        i for i in range(width)
        if not columns[i].startswith("column_") or any(i < len(r) and not _blank(r[i]) for r in grid[header_index + 1:])
    ]  # fmt: skip
    rows = [
        {columns[i]: (row[i].strip() if isinstance(row[i], str) else row[i]) if i < len(row) else None for i in keep}
        for row in grid[header_index + 1:]
    ]  # fmt: skip
    table = Table(name, [columns[i] for i in keep], rows, source=source, header_row=header_index + 1)
    if header_index:
        table.notes.append(f"skipped {header_index} title row(s) above the header")
    return table


def _detect_header(grid: list[list[Any]]) -> int:
    """The first row that looks like labels: mostly non-numeric text, followed by data."""
    for index, row in enumerate(grid[:25]):
        cells = [cell for cell in row if not _blank(cell)]
        if len(cells) < 2:
            continue
        texty = sum(1 for cell in cells if isinstance(cell, str) and parse_number(cell) is None)
        if texty / len(cells) >= 0.7 and index + 1 < len(grid):
            return index
    return 0


# --------------------------------------------------------------------- analysis
def column_type(values: list[Any]) -> str:
    present = [v for v in values if not _blank(v)]
    if not present:
        return "empty"
    if sum(1 for v in present if isinstance(v, datetime | date)) / len(present) >= 0.9:
        return "date"
    if sum(1 for v in present if parse_number(v) is not None) / len(present) >= 0.9:
        return "number"
    return "text"


def profile(table: Table, top: int = 5) -> dict[str, Any]:
    columns: list[dict[str, Any]] = []
    for column in table.columns:
        values = table.values(column)
        present = [v for v in values if not _blank(v)]
        kind = column_type(values)
        info: dict[str, Any] = {
            "name": column, "type": kind, "non_null": len(present), "nulls": len(values) - len(present),
            "unique": len({_norm(v) for v in present}),
        }  # fmt: skip
        if kind == "number":
            numbers = [n for n in (parse_number(v) for v in present) if n is not None]
            if numbers:
                info.update(sum=_round(sum(numbers)), mean=_round(sum(numbers) / len(numbers)),
                            min=_round(min(numbers)), max=_round(max(numbers)))  # fmt: skip
        elif kind == "date":
            dates = sorted(v for v in present if isinstance(v, datetime | date))
            info.update(min=_jsonable(dates[0]), max=_jsonable(dates[-1]))
        elif kind == "text":
            counts = Counter(str(v).strip() for v in present)
            info["top_values"] = [{"value": value, "count": count} for value, count in counts.most_common(top)]
        columns.append(info)
    return {
        "table": table.name, "source": table.source, "rows": len(table), "column_count": len(table.columns),
        "header_row": table.header_row, "columns": columns, "notes": table.notes,
    }  # fmt: skip


def _round(value: float) -> float | int:
    return int(value) if float(value).is_integer() else round(value, 4)


def _norm(value: Any) -> str:
    number = parse_number(value) if not isinstance(value, datetime | date) else None
    if number is not None:
        return str(int(number)) if number.is_integer() else repr(number)
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def find_duplicates(table: Table, keys: list[str] | None = None, sample: int = 20) -> dict[str, Any]:
    columns = [table.resolve(k) for k in keys] if keys else list(table.columns)
    groups: dict[tuple[str, ...], list[int]] = {}
    for index, row in enumerate(table.rows):
        signature = tuple(_norm(row.get(c)) if not _blank(row.get(c)) else "" for c in columns)
        if not any(signature):
            continue
        groups.setdefault(signature, []).append(index)
    repeated = {sig: idx for sig, idx in groups.items() if len(idx) > 1}
    ordered = sorted(repeated.items(), key=lambda item: -len(item[1]))
    return {
        "keys": columns,
        "duplicate_groups": len(repeated),
        "duplicate_rows": sum(len(idx) - 1 for idx in repeated.values()),
        "sample": [
            {"values": {c: _jsonable(table.rows[idx[0]].get(c)) for c in columns}, "count": len(idx),
             "row_numbers": [i + table.header_row + 1 for i in idx[:10]]}
            for _, idx in ordered[:sample]
        ],  # fmt: skip
    }


def filter_rows(table: Table, conditions: list[dict[str, Any]]) -> Table:
    checks = []
    for condition in conditions:
        op = str(condition.get("op", "==")).lower()
        if op not in OPERATORS:
            raise ValidationFailed(f"unknown operator '{op}'", hint="use one of: " + ", ".join(OPERATORS))
        checks.append((table.resolve(str(condition["column"])), op, condition.get("value")))
    rows = [row for row in table.rows if all(_test(row.get(col), op, expected) for col, op, expected in checks)]
    return Table(f"{table.name} (filtered)", list(table.columns), rows, table.source, table.header_row)


def _test(actual: Any, op: str, expected: Any) -> bool:
    if op == "isnull":
        return _blank(actual)
    if op == "notnull":
        return not _blank(actual)
    if _blank(actual):
        return op == "!="
    if op == "in":
        options = {_norm(v) for v in (expected if isinstance(expected, list) else [expected])}
        return _norm(actual) in options
    if op in {"contains", "startswith", "endswith"}:
        text, needle = str(actual).casefold(), str(expected).casefold()
        return needle in text if op == "contains" else getattr(text, op)(needle)
    left, right = parse_number(actual), parse_number(expected)
    if left is not None and right is not None:
        pair: tuple[Any, Any] = (left, right)
    elif isinstance(actual, datetime | date) and isinstance(expected, str):
        pair = (actual.isoformat()[: len(expected)], expected)
    else:
        pair = (_norm(actual), _norm(expected))
    a, b = pair
    return {"==": a == b, "!=": a != b, ">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]


def group_aggregate(table: Table, by: list[str], aggregations: list[dict[str, str]]) -> Table:
    keys = [table.resolve(k) for k in by]
    specs = []
    for spec in aggregations or [{"op": "count"}]:
        op = str(spec.get("op", "count")).lower()
        if op not in AGGREGATIONS:
            raise ValidationFailed(f"unknown aggregation '{op}'", hint="use one of: " + ", ".join(AGGREGATIONS))
        column = table.resolve(spec["column"]) if spec.get("column") else None
        if column is None and op != "count":
            raise ValidationFailed(f"aggregation '{op}' needs a column")
        specs.append((op, column, spec.get("as") or (f"{op}_{column}" if column else "count")))
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    labels: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for row in table.rows:
        signature = tuple(_norm(row.get(k)) if not _blank(row.get(k)) else "" for k in keys)
        buckets.setdefault(signature, []).append(row)
        labels.setdefault(signature, tuple(_jsonable(row.get(k)) if not _blank(row.get(k)) else "(blank)" for k in keys))
    out_rows = []
    for signature, members in buckets.items():
        record: dict[str, Any] = dict(zip(keys, labels[signature], strict=True))
        for op, column, alias in specs:
            record[alias] = _aggregate(op, [m.get(column) for m in members] if column else members)
        out_rows.append(record)
    return Table(f"{table.name} by {', '.join(keys)}", keys + [alias for _, _, alias in specs], out_rows, table.source)


def _aggregate(op: str, values: list[Any]) -> Any:
    if op == "count":
        return len(values)
    present = [v for v in values if not _blank(v)]
    if op == "nunique":
        return len({_norm(v) for v in present})
    numbers = [n for n in (parse_number(v) for v in present) if n is not None]
    if not numbers:
        return None
    result = {"sum": sum(numbers), "mean": sum(numbers) / len(numbers), "min": min(numbers), "max": max(numbers)}[op]
    return _round(result)


def sort_rows(table: Table, by: str, descending: bool = False) -> Table:
    column = table.resolve(by)

    def sort_key(row: dict[str, Any]) -> tuple[int, Any]:
        value = row.get(column)
        if _blank(value):
            return (2, 0)
        number = parse_number(value)
        return (0, number) if number is not None else (1, _norm(value))

    filled = [row for row in table.rows if not _blank(row.get(column))]
    empty = [row for row in table.rows if _blank(row.get(column))]
    ordered = sorted(filled, key=sort_key, reverse=descending) + empty
    return Table(table.name, list(table.columns), ordered, table.source, table.header_row)


# ----------------------------------------------------------------------- export
def export_table(table: Table, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = re.sub(r"[\[\]:*?/\\]", " ", table.name)[:31] or "Sheet1"
        sheet.append(table.columns)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in table.rows:
            sheet.append([_cell(row.get(column)) for column in table.columns])
        for index, column in enumerate(table.columns, start=1):
            longest = max([len(str(column))] + [len(str(r.get(column) or "")) for r in table.rows[:200]])
            sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = min(60, max(10, longest + 2))
        sheet.freeze_panes = "A2"
        workbook.save(path)
    elif suffix == ".csv":
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:  # BOM keeps Hindi readable in Excel
            writer = csv.writer(handle)
            writer.writerow(table.columns)
            for row in table.rows:
                writer.writerow([_jsonable(row.get(column)) for column in table.columns])
    elif suffix == ".json":
        records = [{c: _jsonable(row.get(c)) for c in table.columns} for row in table.rows]
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        raise ValidationFailed(f"cannot export to '{suffix}'", hint="use .xlsx, .csv or .json")
    return path


def _cell(value: Any) -> Any:
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return value


def to_markdown(table: Table, limit: int = 30) -> str:
    if not table.columns:
        return "_(no columns)_"
    lines = ["| " + " | ".join(table.columns) + " |", "|" + "|".join(" --- " for _ in table.columns) + "|"]
    for row in table.rows[:limit]:
        cells = [str(_jsonable(row.get(c)) if not _blank(row.get(c)) else "").replace("|", "\\|") for c in table.columns]
        lines.append("| " + " | ".join(cells) + " |")
    if len(table) > limit:
        lines.append(f"\n_…{len(table) - limit} more rows_")
    return "\n".join(lines)
