"""CARDS 阶段：每篇入库文献抽取 literature_card（贡献/方法/结果/局限/可引要点）。

改造自 DeepSearch literature_review/llm_extraction.py + evidence_matrix.ReviewEvidenceCard：
去掉 verifier_status 与五级证据指针，仅保留 work_id + 卡片内容（设计 §3.2）。
卡片按 work + source_hash 跨项目缓存复用（extractor 角色，便宜 + 长上下文）。

M1 为「摘要级卡片」：只用标题/摘要/元数据。接入 OA 或项目私有全文后升级为全文级。
Draft-first：LLM 不可用时用确定性回退（摘要切句），卡片永远有内容。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from db import find_cached_card, list_entries, upsert_card
from db.models.library import LiteratureCard
from llm_runtime import LLMRunner
from sqlalchemy import select

from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.fulltext import load_persisted_fulltext_sources

MAX_LIST_ITEMS = 12
MAX_SUMMARY_CHARS = 800
MAX_FULLTEXT_PROMPT_CHARS = 100_000

_SYSTEM_PROMPT_ZH = """你是文献卡片抽取助手。只依据给定的标题/摘要/元数据/全文抽取，
**不得**补充给定文本以外的任何事实、数字或结论。只输出 JSON：
{
  "summary": "2-3 句话概述这篇文献做了什么",
  "contributions": ["贡献点"],
  "methods": ["方法/技术"],
  "results": ["结论性发现（只写文中出现的）"],
  "limitations": ["局限（未提及则留空数组）"],
  "quotable_points": [
    {"text": "原文证据片段", "page": 3, "section": "Results", "paragraph": 2}
  ]
}
全文包含 [[PAGE=... | SECTION=...]] 定位标记时必须原样回填页码/章节；
没有可靠定位时相应字段填 null。证据片段应尽量保持原文。
若信息不足，相应数组留空，不要编造。所有 summary、contributions、methods、results 和 limitations
必须用中文输出；原文证据片段可保留原始语言。"""

_SYSTEM_PROMPT_EN = """You extract literature cards. Use ONLY the given
title/abstract/metadata/full text;
never add facts, numbers, or conclusions that are not present in the given text. Output JSON only:
{
  "summary": "2-3 sentences on what this work does",
  "contributions": ["contribution"],
  "methods": ["method/technique"],
  "results": ["findings stated in the text"],
  "limitations": ["limitations, empty array if not stated"],
  "quotable_points": [
    {"text": "verbatim evidence excerpt", "page": 3,
     "section": "Results", "paragraph": 2}
  ]
}
When full text contains [[PAGE=... | SECTION=...]] markers, copy the page/section
locator. Use null when no reliable locator exists. Keep evidence excerpts close to
the source wording. If evidence is thin, return empty arrays — never invent. Write all card
fields in English; only verbatim evidence excerpts may retain their source language."""


@dataclass
class CardsOutcome:
    requested: int = 0
    generated: int = 0
    cached: int = 0
    fallback: int = 0
    fulltext_cards: int = 0
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "generated": self.generated,
            "cached": self.cached,
            "fallback": self.fallback,
            "fulltext_cards": self.fulltext_cards,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class _CardGenerationResult:
    disposition: str
    fulltext_generated: bool = False


def card_source_hash(
    *,
    title: str,
    abstract: str | None,
    fulltext: str | None = None,
    language: str = "en",
) -> str:
    """卡片输入的内容指纹：输入不变则可跨项目复用卡片（设计 §4.3）。"""
    payload = "␟".join([language, title or "", abstract or "", fulltext or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


async def generate_cards(
    context: JobContext,
    *,
    status: str = "selected",
    limit: int | None = None,
    language: str = "en",
    fulltexts: dict[str, str] | None = None,
    work_ids: list[Any] | None = None,
) -> CardsOutcome:
    """为项目内指定状态的文献批量生成卡片。"""
    outcome = CardsOutcome()
    runner = context.llm_runner()

    async with context.session() as session:
        entries = await list_entries(session, context.project_id, status=status)
    if work_ids is not None:
        requested_work_ids = {str(work_id) for work_id in work_ids}
        entries = [(entry, work) for entry, work in entries if str(work.id) in requested_work_ids]
    if limit is not None:
        entries = entries[:limit]
    outcome.requested = len(entries)

    fulltexts = dict(fulltexts or {})
    # Card generation can be run independently from ingest; durable chunks are
    # the source of truth, not a transient worker-process dictionary.
    persisted_sources = await load_persisted_fulltext_sources(
        context, [work.id for _entry, work in entries]
    )
    for work_id, source in persisted_sources.items():
        if source.is_private:
            # The project's explicitly uploaded source wins over a transient OA
            # fetch from the same run.
            fulltexts[work_id] = source.text
        else:
            fulltexts.setdefault(work_id, source.text)

    async def _generate_one(work: Any) -> _CardGenerationResult:
        fulltext = fulltexts.get(str(work.id))
        persisted_source = persisted_sources.get(str(work.id))
        # A caller-provided text that is not the authorized persisted source is
        # conservatively treated as private.  A content hash is never an ACL.
        private_source = bool(fulltext) and (
            persisted_source is None
            or persisted_source.text != fulltext
            or persisted_source.is_private
        )
        source_hash = card_source_hash(
            title=work.canonical_title,
            abstract=work.abstract,
            fulltext=fulltext,
            language=language,
        )
        async with context.session() as session:
            cached = await _find_card_cache(
                session,
                project_id=context.project_id,
                work_id=work.id,
                source_hash=source_hash,
                private_source=private_source,
            )
            if cached is not None and cached.project_id == context.project_id:
                return _CardGenerationResult("cached")
            reused = (
                {
                    "summary": cached.summary,
                    "contributions": cached.contributions_json or [],
                    "methods": cached.methods_json or [],
                    "results": cached.results_json or [],
                    "limitations": cached.limitations_json or [],
                    "quotable_points": cached.quotable_points_json or [],
                    "extraction_model": cached.extraction_model,
                }
                if cached is not None
                else None
            )

        if reused is not None:
            card_data = reused
            disposition = "cached"
        else:
            card_data, used_fallback = await extract_card(
                title=work.canonical_title,
                abstract=work.abstract,
                venue=work.venue_name,
                year=work.publication_year,
                runner=runner,
                language=language,
                fulltext=fulltext,
            )
            disposition = "fallback" if used_fallback else "generated"

        async with context.session() as session:
            await upsert_card(
                session,
                project_id=context.project_id,
                work_id=work.id,
                summary=card_data.get("summary"),
                contributions=card_data.get("contributions"),
                methods=card_data.get("methods"),
                results=card_data.get("results"),
                limitations=card_data.get("limitations"),
                quotable_points=card_data.get("quotable_points"),
                fulltext_used=bool(fulltext),
                extraction_model=card_data.get("extraction_model"),
                source_hash=source_hash,
            )
        return _CardGenerationResult(
            disposition,
            fulltext_generated=bool(fulltext and reused is None),
        )

    # Each card has an independent prompt and persistence key.  Process a small
    # fixed-size batch concurrently, then checkpoint/stop only after every paid
    # call in that batch has been persisted.  This preserves the existing
    # interruption guarantee while removing the N × provider-latency wall time.
    concurrency = max(1, min(int(context.settings.card_concurrency), 12))
    for batch_start in range(0, len(entries), concurrency):
        batch = entries[batch_start : batch_start + concurrency]
        results = await asyncio.gather(*(_generate_one(work) for _entry, work in batch))
        for result in results:
            if result.disposition == "cached":
                outcome.cached += 1
            elif result.disposition == "fallback":
                outcome.fallback += 1
            else:
                outcome.generated += 1
            if result.fulltext_generated:
                outcome.fulltext_cards += 1
        done = batch_start + len(batch)
        await context.emit(
            "cards.progress",
            {"done": done, "total": outcome.requested},
            stage="cards",
            progress=None,
        )
        # Cards in the whole batch are upserted before honoring the stop flag;
        # no completed provider call is discarded.
        await context.raise_if_stopped()
    return outcome


async def _find_card_cache(
    session: Any,
    *,
    project_id: Any,
    work_id: Any,
    source_hash: str,
    private_source: bool,
) -> LiteratureCard | None:
    """Never inspect another project's card when the source text is private."""
    if not private_source:
        return await find_cached_card(
            session,
            work_id=work_id,
            source_hash=source_hash,
        )
    return await session.scalar(
        select(LiteratureCard)
        .where(
            LiteratureCard.project_id == project_id,
            LiteratureCard.work_id == work_id,
            LiteratureCard.source_hash == source_hash,
        )
        .limit(1)
    )


async def extract_card(
    *,
    title: str,
    abstract: str | None,
    venue: str | None = None,
    year: int | None = None,
    runner: LLMRunner | None = None,
    language: str = "en",
    fulltext: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """抽取单张卡片，返回 (card, used_fallback)。"""
    if runner is None or not runner.enabled:
        return deterministic_card(title=title, abstract=abstract), True

    system_prompt = _SYSTEM_PROMPT_ZH if language == "zh" else _SYSTEM_PROMPT_EN
    context_lines = [f"Title: {title}"]
    if year:
        context_lines.append(f"Year: {year}")
    if venue:
        context_lines.append(f"Venue: {venue}")
    context_lines.append(f"Abstract: {abstract or '(no abstract available)'}")
    if fulltext:
        # 全文级卡片：来源可能是共享 OA，也可能是用户确认的项目私有 PDF。
        context_lines.append(
            f"Full text (authorized source):\n{fulltext[:MAX_FULLTEXT_PROMPT_CHARS]}"
        )
    result = await runner.agenerate_json(
        "extractor",
        system_prompt=system_prompt,
        user_prompt="\n".join(context_lines),
        max_output_tokens=2600 if fulltext else 1200,
        temperature=0.1,
        metadata={"stage": "cards"},
    )
    if not result.ok or not isinstance(result.value, dict):
        return deterministic_card(title=title, abstract=abstract), True
    card = normalize_card(result.value)
    card["extraction_model"] = result.model or runner.model_for("extractor")
    return card, False


def normalize_card(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": _clean_text(raw.get("summary"))[:MAX_SUMMARY_CHARS] or None,
        "contributions": _clean_list(raw.get("contributions")),
        "methods": _clean_list(raw.get("methods")),
        "results": _clean_list(raw.get("results")),
        "limitations": _clean_list(raw.get("limitations")),
        "quotable_points": _clean_evidence_points(raw.get("quotable_points")),
    }


def deterministic_card(*, title: str, abstract: str | None) -> dict[str, Any]:
    """确定性回退：只做摘要切句，不产生任何摘要之外的断言。"""
    sentences = _split_sentences(abstract or "")
    return {
        "summary": (" ".join(sentences[:3]) or title)[:MAX_SUMMARY_CHARS],
        "contributions": [],
        "methods": [],
        "results": [],
        "limitations": [],
        # 可引要点直接取摘要原句：可引用且绝不改写事实。
        "quotable_points": [
            {"text": sentence, "page": None, "section": "abstract", "paragraph": index + 1}
            for index, sentence in enumerate(sentences[:MAX_LIST_ITEMS])
        ],
        "extraction_model": "deterministic_abstract_v1",
    }


def _split_sentences(text: str) -> list[str]:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?。！？])\s+", cleaned)
    return [part.strip() for part in parts if len(part.strip()) > 20]


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [_clean_text(item) for item in value]
    return [item for item in items if item][:MAX_LIST_ITEMS]


def _clean_evidence_points(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    points: list[dict[str, Any]] = []
    for item in value:
        point: dict[str, Any]
        if isinstance(item, str):
            text = _clean_text(item)
            point = {"text": text, "page": None, "section": None, "paragraph": None}
        elif isinstance(item, dict):
            text = _clean_text(item.get("text") or item.get("excerpt"))
            page = item.get("page")
            paragraph = item.get("paragraph")
            point = {
                "text": text,
                "page": page if isinstance(page, int) and page > 0 else None,
                "section": _clean_text(item.get("section")) or None,
                "paragraph": paragraph if isinstance(paragraph, int) and paragraph > 0 else None,
            }
        else:
            continue
        if point["text"]:
            points.append(point)
    return points[:MAX_LIST_ITEMS]
