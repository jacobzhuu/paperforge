"""稳定切块（段落窗口 v1）。

迁移自 DeepSearch parsing/chunking.py（零改动，设计 §3.1）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_CHARS_PER_CHUNK = 1_200


@dataclass(frozen=True)
class ParsedChunk:
    chunk_no: int
    text: str
    token_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


def chunk_text(
    text: str,
    *,
    max_chars_per_chunk: int = DEFAULT_MAX_CHARS_PER_CHUNK,
) -> list[ParsedChunk]:
    normalized_text = text.strip()
    if not normalized_text:
        return []

    paragraphs = [item.strip() for item in re.split(r"\n{2,}", normalized_text) if item.strip()]
    if not paragraphs:
        paragraphs = [normalized_text]

    chunk_payloads: list[tuple[str, int]] = []
    current_paragraphs: list[str] = []
    current_paragraph_count = 0

    for paragraph in paragraphs:
        paragraph_segments = _split_long_paragraph(paragraph, max_chars=max_chars_per_chunk)
        for segment in paragraph_segments:
            candidate_parts = current_paragraphs + [segment]
            candidate_text = "\n\n".join(candidate_parts)
            if current_paragraphs and len(candidate_text) > max_chars_per_chunk:
                chunk_payloads.append(("\n\n".join(current_paragraphs), current_paragraph_count))
                current_paragraphs = [segment]
                current_paragraph_count = 1
                continue
            current_paragraphs.append(segment)
            current_paragraph_count += 1

    if current_paragraphs:
        chunk_payloads.append(("\n\n".join(current_paragraphs), current_paragraph_count))

    chunks: list[ParsedChunk] = []
    cursor = 0
    for chunk_no, (chunk_text_value, paragraph_count) in enumerate(chunk_payloads):
        char_count = len(chunk_text_value)
        token_count = max(1, math.ceil(char_count / 4))
        start_offset = normalized_text.find(chunk_text_value, cursor)
        if start_offset < 0:
            start_offset = cursor
        end_offset = start_offset + char_count
        cursor = end_offset
        chunks.append(
            ParsedChunk(
                chunk_no=chunk_no,
                text=chunk_text_value,
                token_count=token_count,
                metadata={
                    "strategy": "paragraph_window_v1",
                    "char_count": char_count,
                    "char_start": start_offset,
                    "char_end": end_offset,
                    "paragraph_count": paragraph_count,
                    "approx_token_count": token_count,
                },
            )
        )
    return chunks


def _split_long_paragraph(paragraph: str, *, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]

    segments: list[str] = []
    remaining = paragraph
    while len(remaining) > max_chars:
        split_at = remaining.rfind(" ", 0, max_chars + 1)
        if split_at <= 0 or split_at < max_chars // 2:
            split_at = max_chars
        segment = remaining[:split_at].strip()
        if segment:
            segments.append(segment)
        remaining = remaining[split_at:].strip()
    if remaining:
        segments.append(remaining)
    return segments
