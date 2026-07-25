# 迁移自 DeepSearch literature_review/section_chunks.py（纯函数，零改动）。
# 服务于文献卡片抽取：section 线索组、内容角色分类、Markdown 表格解析、字符预算选块。
"""Section-aware full-text chunk selection for literature-review extraction."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# Cue groups ordered from highest extraction value to lowest.
SECTION_CUE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "methods",
        (
            r"\bmethods?\b",
            r"\bapproach\b",
            r"\bexperimental\s+setup\b",
        ),
    ),
    (
        "results",
        (
            r"\bresults?\b",
            r"\bexperiments?\b",
            r"\bevaluation\b",
            r"\bfindings?\b",
        ),
    ),
    (
        "discussion",
        (
            r"\blimitations?\b",
            r"\bdiscussion\b",
            r"\bconclusions?\b",
            r"\bfuture\s+work\b",
        ),
    ),
    (
        "intro",
        (
            r"\babstract\b",
            r"\bintroduction\b",
        ),
    ),
)

_DEFAULT_MAX_CHUNKS = 4
_DEFAULT_CHAR_BUDGET = 12_000
_HEAD_CHARS = 200


class ContentRole(StrEnum):
    SCIENTIFIC_PROSE = "scientific_prose"
    STRUCTURED_TABLE = "structured_table"
    REFERENCES = "references"
    AUTHOR_AFFILIATION = "author_affiliation"
    ACKNOWLEDGEMENTS = "acknowledgements"
    NAVIGATION = "navigation"
    PDF_DEBRIS = "pdf_debris"
    CITATION_ONLY = "citation_only"
    LOW_LANGUAGE_DENSITY = "low_language_density"
    MALFORMED_TABLE = "malformed_table"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class ContentRoleDecision:
    role: ContentRole
    claim_eligible: bool
    reason: str
    confidence: float


@dataclass(frozen=True)
class StructuredTableCell:
    row_index: int
    column_index: int
    column_name: str
    value: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class StructuredTableEvidence:
    columns: tuple[str, ...]
    cells: tuple[StructuredTableCell, ...]


@dataclass(frozen=True)
class ChunkText:
    chunk_id: Any
    text: str

    @property
    def char_len(self) -> int:
        return len(self.text or "")


def classify_content_role(text: str) -> ContentRoleDecision:
    """Classify whether parser output is eligible to support a scientific finding."""
    value = (text or "").strip()
    lower = value.casefold()
    if not value:
        return ContentRoleDecision(ContentRole.UNCLASSIFIED, False, "empty", 1.0)
    if re.match(r"^(references|bibliography|参考文献)\b", lower):
        return ContentRoleDecision(ContentRole.REFERENCES, False, "reference_section", 0.99)
    if re.match(r"^(acknowledg(e)?ments?|致谢)\b", lower):
        return ContentRoleDecision(ContentRole.ACKNOWLEDGEMENTS, False, "acknowledgements", 0.99)
    if re.search(r"preparing to download|download pdf|view article|cookie preferences", lower):
        return ContentRoleDecision(ContentRole.NAVIGATION, False, "navigation_placeholder", 0.99)
    if re.search(r"(?:^|\s)(?:obj|endobj|xref|stream|endstream)(?:\s|$)", lower):
        return ContentRoleDecision(ContentRole.PDF_DEBRIS, False, "pdf_object_debris", 0.99)
    if "|" in value and len(value.splitlines()) >= 3:
        table = parse_markdown_table(value)
        if table is not None:
            return ContentRoleDecision(ContentRole.STRUCTURED_TABLE, True, "structured_table", 0.95)
        return ContentRoleDecision(ContentRole.MALFORMED_TABLE, False, "unrecoverable_table", 0.96)
    if _looks_like_author_affiliation(value):
        return ContentRoleDecision(
            ContentRole.AUTHOR_AFFILIATION, False, "author_affiliation_block", 0.95
        )
    if _looks_citation_only(value):
        return ContentRoleDecision(ContentRole.CITATION_ONLY, False, "citation_only", 0.96)
    letter_count = sum(character.isalpha() for character in value)
    visible_count = sum(not character.isspace() for character in value)
    if visible_count and letter_count / visible_count < 0.35:
        role = (
            ContentRole.MALFORMED_TABLE
            if sum(c.isdigit() for c in value) >= 6
            else ContentRole.LOW_LANGUAGE_DENSITY
        )
        return ContentRoleDecision(role, False, "insufficient_language_density", 0.94)
    words = re.findall(r"\b[^\W\d_]{2,}\b", value, flags=re.UNICODE)
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", value))
    if len(words) < 3 and cjk_count < 8:
        return ContentRoleDecision(
            ContentRole.UNCLASSIFIED, False, "insufficient_scientific_prose", 0.8
        )
    return ContentRoleDecision(ContentRole.SCIENTIFIC_PROSE, True, "eligible_prose", 0.85)


def parse_markdown_table(text: str) -> StructuredTableEvidence | None:
    """Parse a bounded Markdown table while retaining row/cell source locators."""
    lines = text.splitlines(keepends=True)
    if len(lines) < 3:
        return None
    parsed_lines = [_table_cells(line) for line in lines]
    if any(cells is None for cells in parsed_lines[:3]):
        return None
    header = parsed_lines[0] or []
    separator = parsed_lines[1] or []
    if len(header) < 2 or len(separator) != len(header):
        return None
    if not all(re.fullmatch(r":?-{3,}:?", cell[0].strip()) for cell in separator):
        return None
    rows = parsed_lines[2:]
    if not rows or any(row is None or len(row) != len(header) for row in rows):
        return None
    columns = tuple(cell[0].strip() for cell in header)
    if any(not column for column in columns):
        return None
    cells: list[StructuredTableCell] = []
    line_offset = sum(len(line) for line in lines[:2])
    for row_index, (line, row) in enumerate(zip(lines[2:], rows, strict=True), start=1):
        assert row is not None
        for column_index, (value, local_start, local_end) in enumerate(row):
            cells.append(
                StructuredTableCell(
                    row_index=row_index,
                    column_index=column_index,
                    column_name=columns[column_index],
                    value=value.strip(),
                    start_offset=line_offset + local_start,
                    end_offset=line_offset + local_end,
                )
            )
        line_offset += len(line)
    return StructuredTableEvidence(columns=columns, cells=tuple(cells))


def _table_cells(line: str) -> list[tuple[str, int, int]] | None:
    raw = line.rstrip("\r\n")
    if "|" not in raw:
        return None
    left = 1 if raw.startswith("|") else 0
    right = len(raw) - 1 if raw.endswith("|") else len(raw)
    body = raw[left:right]
    parts: list[tuple[str, int, int]] = []
    cursor = left
    for value in body.split("|"):
        start = cursor
        end = start + len(value)
        parts.append((value, start, end))
        cursor = end + 1
    return parts


def score_chunk_for_section_group(text: str, group: str) -> float:
    """Score a chunk for one cue group. Head matches weigh more than body-only."""
    patterns = _patterns_for_group(group)
    if not patterns or not text:
        return 0.0
    head = text[:_HEAD_CHARS].casefold()
    body = text.casefold()
    score = 0.0
    for pattern in patterns:
        if pattern.search(head):
            score += 3.0
        elif pattern.search(body):
            score += 1.0
    # Intro/abstract cues are intentionally weaker than methods/results/discussion.
    if group == "intro":
        score *= 0.5
    return score


def select_section_aware_chunks(
    chunks: Sequence[ChunkText],
    *,
    max_chunks: int = _DEFAULT_MAX_CHUNKS,
    char_budget: int = _DEFAULT_CHAR_BUDGET,
) -> list[ChunkText]:
    """Pick up to ``max_chunks`` section-aware excerpts within ``char_budget``.

    Selection is restricted to claim-eligible scientific prose and chooses the best-scoring
    chunk per cue group (methods → results → discussion → intro). Unclassified text is never
    promoted merely because it is long. The final chunk is truncated if needed.
    """
    if not chunks:
        return []
    usable = [
        item
        for item in chunks
        if (item.text or "").strip() and classify_content_role(item.text).claim_eligible
    ]
    if not usable:
        return []

    selected: list[ChunkText] = []
    selected_ids: set[Any] = set()

    for group_name, _patterns in SECTION_CUE_GROUPS:
        if len(selected) >= max_chunks:
            break
        best: ChunkText | None = None
        best_score = 0.0
        for chunk in usable:
            if chunk.chunk_id in selected_ids:
                continue
            score = score_chunk_for_section_group(chunk.text, group_name)
            if score > best_score:
                best = chunk
                best_score = score
        if best is not None and best_score > 0:
            selected.append(best)
            selected_ids.add(best.chunk_id)

    return _apply_char_budget(selected, char_budget=char_budget)


def _apply_char_budget(chunks: list[ChunkText], *, char_budget: int) -> list[ChunkText]:
    if char_budget <= 0 or not chunks:
        return []
    kept: list[ChunkText] = []
    used = 0
    for index, chunk in enumerate(chunks):
        remaining = char_budget - used
        if remaining <= 0:
            break
        text = chunk.text or ""
        if len(text) <= remaining:
            kept.append(chunk)
            used += len(text)
            continue
        # Truncate only the last admitted chunk.
        if index == 0 or kept:
            kept.append(ChunkText(chunk_id=chunk.chunk_id, text=text[:remaining]))
        break
    return kept


def _patterns_for_group(group: str) -> tuple[re.Pattern[str], ...]:
    for name, raw_patterns in SECTION_CUE_GROUPS:
        if name == group:
            return tuple(re.compile(pattern, re.IGNORECASE) for pattern in raw_patterns)
    return ()


def _looks_like_author_affiliation(value: str) -> bool:
    lower = value.casefold()
    affiliation_terms = (
        "university",
        "department",
        "institute",
        "laboratory",
        "corresponding author",
    )
    email_or_orcid = bool(re.search(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|\borcid\b", lower))
    comma_density = value.count(",") >= 4
    if (email_or_orcid and any(term in lower for term in affiliation_terms)) or (
        comma_density and sum(term in lower for term in affiliation_terms) >= 2
    ):
        return True
    name_segments = [segment.strip(" .…") for segment in value.split(",") if segment.strip()]
    if len(name_segments) >= 2 and ("..." in value or "…" in value):
        name_like = sum(
            1
            for segment in name_segments
            if 2 <= len(segment.split()) <= 4
            and all(part[:1].isupper() for part in segment.split() if part)
        )
        return name_like == len(name_segments)
    return False


def _looks_citation_only(value: str) -> bool:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return False
    citation_lines = sum(
        bool(
            re.match(r"^\[?\d{1,4}\]?\s*[.)]?\s*", line)
            and re.search(r"\b(?:19|20)\d{2}\b|\bdoi\b|\bet al\.", line.casefold())
        )
        for line in lines
    )
    return citation_lines / len(lines) >= 0.6
