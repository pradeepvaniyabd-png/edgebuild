"""Document reading and writing: PDF, DOCX, text, JSON, tables and image metadata.

Text extraction only reads a document's text layer. Scanned PDFs and photos need
OCR, which this module does not include; it reports that plainly instead of guessing.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..errors import FileProblem
from .table import load_tables, profile, to_markdown

TEXT_SUFFIXES = {
    ".txt", ".md", ".rst", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".sql", ".sh", ".xml", ".srt", ".vtt",
}  # fmt: skip
TABLE_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".tsv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}

_SENTENCE = re.compile(r"(?<=[.!?।])\s+")
_WORD = re.compile(r"[\wऀ-ॿ]+", re.UNICODE)
_STOPWORDS = frozenset(
    """a an the and or but if then of in on at to for from by with as is are was were be been being this that these
    those it its he she they we you i not no so do does did has have had will would can could should may might
    about into over under more most very just than also only such
    है हैं था थी थे और या का की के को से में पर यह वह ये वे एक भी नहीं तो ही कि जो तथा एवं लिए गया गई किया कर""".split()
)


class Extraction(BaseModel):
    path: str
    kind: str
    text: str = ""
    pages: int | None = None
    tables: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def characters(self) -> int:
        return len(self.text)


def read_text_file(path: Path, max_bytes: int = 5_000_000) -> str:
    data = path.read_bytes()[:max_bytes]
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", errors="replace")


def extract(path: Path, *, max_chars: int = 400_000) -> Extraction:
    if not path.is_file():
        raise FileProblem(f"file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        result = _pdf(path)
    elif suffix == ".docx":
        result = _docx(path)
    elif suffix in TABLE_SUFFIXES:
        result = _tables(path)
    elif suffix == ".json":
        result = _json(path)
    elif suffix in IMAGE_SUFFIXES:
        result = _image(path)
    elif suffix in TEXT_SUFFIXES or suffix == "":
        result = Extraction(path=str(path), kind="text", text=read_text_file(path))
    elif suffix == ".doc":
        raise FileProblem("legacy .doc is not supported", hint="save the document as .docx or .pdf and retry")
    else:
        raise FileProblem(f"unsupported document type '{suffix}'")
    if len(result.text) > max_chars:
        result.warnings.append(f"text truncated to {max_chars} of {len(result.text)} characters")
        result.text = result.text[:max_chars]
    return result


def _pdf(path: Path) -> Extraction:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise FileProblem(f"{path.name} is password-protected") from exc
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, OSError, ValueError, KeyError) as exc:
        raise FileProblem(f"cannot read PDF {path.name}: {exc}") from exc
    text = "\n\n".join(f"[page {i + 1}]\n{body.strip()}" for i, body in enumerate(pages) if body.strip())
    result = Extraction(path=str(path), kind="pdf", text=text, pages=len(pages))
    info = reader.metadata or {}
    result.metadata = {k.lstrip("/").lower(): str(v) for k, v in info.items() if v}
    empty = sum(1 for body in pages if not body.strip())
    if pages and empty == len(pages):
        result.warnings.append("no text layer found: this looks like a scanned PDF and needs OCR, which is not available")
    elif empty:
        result.warnings.append(f"{empty} of {len(pages)} pages had no extractable text (possibly scanned)")
    return result


def _docx(path: Path) -> Extraction:
    import docx
    from docx.opc.exceptions import PackageNotFoundError

    try:
        document = docx.Document(str(path))
    except (PackageNotFoundError, KeyError, ValueError, OSError) as exc:
        raise FileProblem(f"cannot read DOCX {path.name}: {exc}") from exc
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
    tables = []
    for index, table in enumerate(document.tables):
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        tables.append({"index": index, "rows": rows})
        paragraphs.append("\n".join(" | ".join(row) for row in rows))
    props = document.core_properties
    return Extraction(
        path=str(path), kind="docx", text="\n\n".join(paragraphs), tables=tables,
        metadata={k: str(v) for k, v in {"title": props.title, "author": props.author}.items() if v},
    )  # fmt: skip


def _tables(path: Path) -> Extraction:
    tables = load_tables(path)
    profiles = [profile(table) for table in tables]
    text = "\n\n".join(f"## {t.name} ({len(t)} rows)\n{to_markdown(t, limit=25)}" for t in tables)
    return Extraction(path=str(path), kind="table", text=text, tables=profiles)


def _json(path: Path) -> Extraction:
    raw = read_text_file(path)
    try:
        text = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except ValueError:
        return Extraction(path=str(path), kind="json", text=raw, warnings=["file is not valid JSON; returned as text"])
    return Extraction(path=str(path), kind="json", text=text)


def _image(path: Path) -> Extraction:
    from ..media.probe import probe_image

    info = probe_image(path)
    return Extraction(
        path=str(path), kind="image",
        metadata={"format": info.format, "width": info.width, "height": info.height, "mode": info.mode},
        warnings=["images are described by metadata only; reading text inside an image needs OCR or a vision model"],
    )  # fmt: skip


# ------------------------------------------------------------------ summarising
def split_sentences(text: str) -> list[str]:
    parts = [part.strip() for block in text.splitlines() for part in _SENTENCE.split(block)]
    return [part for part in parts if len(part) > 2]


def keywords(text: str, count: int = 12) -> list[str]:
    words = [w.lower() for w in _WORD.findall(text) if len(w) > 2 and w.lower() not in _STOPWORDS and not w.isdigit()]
    return [word for word, _ in Counter(words).most_common(count)]


def extractive_summary(text: str, max_sentences: int = 6) -> str:
    """Pick the sentences that carry the most frequent content words, in original order.

    A deterministic stand-in when no language model is configured: it selects, it does
    not paraphrase or reason.
    """
    sentences = split_sentences(text)
    if len(sentences) <= max_sentences:
        return " ".join(sentences)
    frequency = Counter(w.lower() for w in _WORD.findall(text) if w.lower() not in _STOPWORDS and len(w) > 2)
    if not frequency:
        return " ".join(sentences[:max_sentences])
    top = frequency.most_common(1)[0][1]

    def score(indexed: tuple[int, str]) -> float:
        index, sentence = indexed
        words = [w.lower() for w in _WORD.findall(sentence)]
        if len(words) < 4:
            return 0.0
        weight = sum(frequency.get(w, 0) / top for w in words) / (len(words) ** 0.5)
        return weight * (1.15 if index < 3 else 1.0)  # opening sentences usually frame the document

    ranked = sorted(enumerate(sentences), key=score, reverse=True)[:max_sentences]
    return " ".join(sentence for _, sentence in sorted(ranked))


# ---------------------------------------------------------------------- writing
def write_docx(path: Path, title: str, sections: list[dict[str, Any]], *, font: str | None = None) -> Path:
    """Write a simple report. Each section: {heading?, text?, bullets?, table?: {columns, rows}}."""
    import docx
    from docx.shared import Pt

    document = docx.Document()
    if font:
        style = document.styles["Normal"]
        style.font.name = font
        style.font.size = Pt(11)
    if title:
        document.add_heading(title, level=0)
    for section in sections:
        if section.get("heading"):
            document.add_heading(str(section["heading"]), level=1)
        for paragraph in str(section.get("text") or "").split("\n\n"):
            if paragraph.strip():
                document.add_paragraph(paragraph.strip())
        for bullet in section.get("bullets") or []:
            document.add_paragraph(str(bullet), style="List Bullet")
        table = section.get("table")
        if table and table.get("columns"):
            grid = document.add_table(rows=1, cols=len(table["columns"]))
            grid.style = "Table Grid"
            for cell, label in zip(grid.rows[0].cells, table["columns"], strict=True):
                cell.text = str(label)
                for run in cell.paragraphs[0].runs:
                    run.bold = True
            for row in table.get("rows", []):
                cells = grid.add_row().cells
                for cell, column in zip(cells, table["columns"], strict=True):
                    value = row.get(column) if isinstance(row, dict) else None
                    cell.text = "" if value is None else str(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    return path


def write_markdown(path: Path, title: str, sections: list[dict[str, Any]]) -> Path:
    lines = [f"# {title}", ""] if title else []
    for section in sections:
        if section.get("heading"):
            lines += [f"## {section['heading']}", ""]
        if section.get("text"):
            lines += [str(section["text"]).strip(), ""]
        for bullet in section.get("bullets") or []:
            lines.append(f"- {bullet}")
        if section.get("bullets"):
            lines.append("")
        table = section.get("table")
        if table and table.get("columns"):
            columns = table["columns"]
            lines.append("| " + " | ".join(str(c) for c in columns) + " |")
            lines.append("|" + "|".join(" --- " for _ in columns) + "|")
            for row in table.get("rows", []):
                lines.append("| " + " | ".join(str(row.get(c, "") if row.get(c) is not None else "") for c in columns) + " |")
            lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path
