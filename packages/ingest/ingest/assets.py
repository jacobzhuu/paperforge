"""用户素材确定性解析（设计 §4.4.2 INPUT 阶段）。

结果表格(CSV/XLSX)、图(PNG/PDF/SVG)、方法笔记(MD/DOCX)、代码片段、已有 .bib
一律**确定性解析**入 `user_asset.parsed_json`——这是「正文数字只能来自素材」
这条红线的唯一事实来源：LLM 不参与解析，也不允许改写解析出的数值。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

from ingest.document_extractors import normalize_mime_type
from ingest.extract import try_extract_and_chunk

ASSET_KINDS = ("dataset", "result_table", "figure", "method_note", "code", "bib")

TABLE_MIME_TYPES = frozenset(
    {
        "text/csv",
        "application/csv",
        "text/tab-separated-values",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)
FIGURE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/svg+xml", "application/pdf"}
)
NOTE_MIME_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
BIB_MIME_TYPES = frozenset({"application/x-bibtex", "text/x-bibtex"})

MAX_TABLE_ROWS = 500
MAX_TABLE_COLUMNS = 40
# 数值识别：整数/小数/百分数/科学计数，允许千分位与正负号。
_NUMBER_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?%?$")


@dataclass
class ParsedAsset:
    kind: str
    parsed: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.parsed)


def infer_asset_kind(*, filename: str, mime_type: str, declared: str | None = None) -> str:
    """按扩展名/mime 推断素材类型；显式声明优先。"""
    if declared in ASSET_KINDS:
        return declared
    normalized = normalize_mime_type(mime_type)
    lower = filename.lower()
    if lower.endswith(".bib") or normalized in BIB_MIME_TYPES:
        return "bib"
    if normalized in TABLE_MIME_TYPES or lower.endswith((".csv", ".tsv", ".xlsx")):
        return "result_table"
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".eps", ".tif", ".tiff")):
        return "figure"
    if normalized in FIGURE_MIME_TYPES and not lower.endswith(".pdf"):
        return "figure"
    if lower.endswith((".py", ".r", ".m", ".ipynb", ".sh", ".java", ".cpp", ".go", ".rs")):
        return "code"
    return "method_note"


def parse_asset(
    *,
    content: bytes,
    filename: str,
    mime_type: str,
    kind: str | None = None,
) -> ParsedAsset:
    """把素材解析成结构化 JSON。解析失败返回带 warnings 的空结果（draft-first）。"""
    resolved = infer_asset_kind(filename=filename, mime_type=mime_type, declared=kind)
    normalized_mime = normalize_mime_type(mime_type)

    if resolved in {"result_table", "dataset"}:
        return parse_table(
            content=content,
            filename=filename,
            mime_type=normalized_mime,
            kind=resolved,
        )
    if resolved == "figure":
        return ParsedAsset(
            kind="figure",
            parsed={
                "type": "figure",
                "filename": filename,
                "mime_type": normalized_mime,
                "bytes": len(content),
            },
        )
    if resolved == "bib":
        return parse_bib(content=content, filename=filename)
    if resolved == "code":
        text = content.decode("utf-8", errors="replace")
        return ParsedAsset(
            kind="code",
            parsed={
                "type": "code",
                "filename": filename,
                "line_count": text.count("\n") + 1,
                "text": text[:20000],
            },
        )
    return parse_note(content=content, filename=filename, mime_type=normalized_mime)


def parse_table(
    *,
    content: bytes,
    filename: str,
    mime_type: str,
    kind: str = "result_table",
) -> ParsedAsset:
    """CSV/TSV/XLSX → 表头 + 行 + 数值索引。

    ``numeric_cells`` 是数字 lint 与确定性注入的事实来源：
    键为 ``行标签::列名``，值为**原样字符串**（不做四舍五入，避免精度漂移）。
    """
    warnings: list[str] = []
    rows: list[list[str]] = []

    if mime_type.endswith("spreadsheetml.sheet") or filename.lower().endswith(".xlsx"):
        parsed, chunks, error = try_extract_and_chunk(mime_type=mime_type, content=content)
        del chunks
        if parsed is None:
            return ParsedAsset(kind=kind, warnings=[f"xlsx parse failed: {error}"])
        rows = _rows_from_cell_text(parsed.text)
    else:
        text = content.decode("utf-8-sig", errors="replace")
        first_line = text.split("\n")[0]
        is_tsv = filename.lower().endswith(".tsv") or "\t" in first_line
        delimiter = "\t" if is_tsv else ","
        for cells in csv.reader(io.StringIO(text), delimiter=delimiter):
            if any(cell.strip() for cell in cells):
                rows.append([cell.strip() for cell in cells])

    if not rows:
        return ParsedAsset(kind=kind, warnings=["table has no rows"])
    if len(rows) > MAX_TABLE_ROWS + 1:
        warnings.append(f"table truncated to {MAX_TABLE_ROWS} rows")
        rows = rows[: MAX_TABLE_ROWS + 1]

    headers = rows[0][:MAX_TABLE_COLUMNS]
    body = [row[: len(headers)] for row in rows[1:]]
    numeric_cells: dict[str, str] = {}
    for row in body:
        if not row:
            continue
        label = row[0]
        for index, cell in enumerate(row[1:], start=1):
            if index >= len(headers):
                break
            if is_number(cell):
                numeric_cells[f"{label}::{headers[index]}"] = cell

    return ParsedAsset(
        kind=kind,
        parsed={
            "type": "table",
            "filename": filename,
            "headers": headers,
            "rows": body,
            "row_count": len(body),
            "column_count": len(headers),
            "numeric_cells": numeric_cells,
            # 全部出现过的数值 token：数字 lint 的白名单。
            "numbers": sorted({normalize_number(v) for v in numeric_cells.values()}),
        },
        warnings=warnings,
    )


def parse_note(*, content: bytes, filename: str, mime_type: str) -> ParsedAsset:
    parsed, chunks, error = try_extract_and_chunk(mime_type=mime_type, content=content)
    if parsed is None:
        text = content.decode("utf-8", errors="replace")
        if not text.strip():
            return ParsedAsset(kind="method_note", warnings=[f"note parse failed: {error}"])
        return ParsedAsset(
            kind="method_note",
            parsed={
                "type": "note",
                "filename": filename,
                "text": text[:50000],
                # 回退路径同样要登记数值：漏了会让笔记里的真实数字被 NUMLINT 误判为编造。
                "numbers": sorted(set(extract_numbers(text))),
            },
            warnings=[f"fell back to raw text: {error}"] if error else [],
        )
    return ParsedAsset(
        kind="method_note",
        parsed={
            "type": "note",
            "filename": filename,
            "title": parsed.title,
            "text": parsed.text[:50000],
            "chunk_count": len(chunks),
            # 笔记里的数值同样是可引用事实来源（例如「样本量 N=128」）。
            "numbers": sorted(set(extract_numbers(parsed.text))),
        },
    )


def parse_bib(*, content: bytes, filename: str) -> ParsedAsset:
    """.bib 只解析为**待核验线索**，绝不直接入库（R1）。"""
    text = content.decode("utf-8", errors="replace")
    try:
        from paper_ir import parse_bibtex_entries

        entries = parse_bibtex_entries(text)
    except (ImportError, ValueError) as error:
        return ParsedAsset(kind="bib", warnings=[f"bibtex parse failed: {error}"])
    return ParsedAsset(
        kind="bib",
        parsed={
            "type": "bib",
            "filename": filename,
            "entry_count": len(entries),
            "entries": [
                {
                    "key": entry.key,
                    "title": entry.title,
                    "doi": entry.doi,
                    "arxiv_id": entry.arxiv_id,
                    "year": entry.publication_year,
                    "authors": list(entry.authors),
                }
                for entry in entries
            ],
            "verification_required": True,
        },
    )


def is_number(value: str) -> bool:
    return bool(_NUMBER_RE.match((value or "").strip()))


def normalize_number(value: str) -> str:
    """归一数值 token 供比对：去千分位、去正号、保留原始精度。"""
    text = (value or "").strip().replace(",", "")
    if text.startswith("+"):
        text = text[1:]
    return text


def extract_numbers(text: str) -> list[str]:
    """抽出文本中的数值 token（数字 lint 用）。"""
    tokens = re.findall(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?%?", text or "")
    return [normalize_number(token) for token in tokens]


_CELL_RE = re.compile(r"([A-Z]{1,3})(\d+):\s*([^|]*)")


def _rows_from_cell_text(text: str) -> list[list[str]]:
    """把 ``A1: model | B1: accuracy A2: ours | B2: 0.931`` 还原成二维表。

    xlsx 抽取器输出的是「坐标: 值」序列（跨行也用同一分隔符），
    因此按行号分组、按列号排序才能拿回真正的表结构。
    """
    grid: dict[int, dict[int, str]] = {}
    for column, row_index, value in _CELL_RE.findall(text or ""):
        grid.setdefault(int(row_index), {})[_column_index(column)] = value.strip()
    rows: list[list[str]] = []
    for row_index in sorted(grid):
        cells = grid[row_index]
        width = max(cells) + 1 if cells else 0
        rows.append([cells.get(index, "") for index in range(width)])
    return [row for row in rows if any(row)]


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


__all__ = [
    "ASSET_KINDS",
    "ParsedAsset",
    "extract_numbers",
    "infer_asset_kind",
    "is_number",
    "normalize_number",
    "parse_asset",
    "parse_bib",
    "parse_note",
    "parse_table",
]
