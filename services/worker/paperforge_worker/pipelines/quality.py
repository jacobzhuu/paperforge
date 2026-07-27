"""质量增强：语义引用软校验 + 覆盖建议 + 质量评分（设计 §4.4.3 可选软校验 / §3.4）。

草稿模式下三者仍是提示；投稿模式在同一份报告上应用确定性硬门槛：
- 语义软校验：cheap 模型比对引用上下文与文献摘要的相关性，低分给黄色徽章；
- 覆盖建议：引用密度低 / 缺近年文献 / 某主题未覆盖 —— 由 gap_analysis 降级而来；
- 质量评分：引用密度、覆盖度、新旧文献比、连贯性，只呈现不设门槛
  （取代旧系统 formal completion 的 12 项硬门槛，见设计 §3.4）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from llm_runtime import LLMRunner

SOFT_CHECK_THRESHOLD = 0.5
MAX_SOFT_CHECKS = 40
RECENT_YEARS_WINDOW = 5

_SOFT_CHECK_PROMPT = """判断每条「引用位置的上下文」与「被引文献摘要」是否语义相关。
只输出 JSON：{"judgements": [{"index": 0, "score": 0.0-1.0, "reason": "简短理由"}]}
score 表示该文献能否支撑该处论述；无法判断时给 0.5 并说明。不要臆测摘要之外的内容。"""


@dataclass
class SoftCheckFinding:
    cite_key: str
    section_key: str
    score: float
    reason: str = ""
    context: str = ""

    @property
    def weak(self) -> bool:
        return self.score < SOFT_CHECK_THRESHOLD

    def to_payload(self) -> dict[str, Any]:
        return {
            "cite_key": self.cite_key,
            "section_key": self.section_key,
            "score": round(self.score, 3),
            "reason": self.reason,
            "context": self.context[:200],
            "weak": self.weak,
        }


@dataclass
class QualityReport:
    section_count: int = 0
    word_count: int = 0
    cite_count: int = 0
    unique_cite_count: int = 0
    whitelist_size: int = 0
    citation_density: float = 0.0
    library_coverage: float = 0.0
    recent_ratio: float = 0.0
    fulltext_coverage: float = 0.0
    sections_without_citations: list[str] = field(default_factory=list)
    soft_check: list[dict[str, Any]] = field(default_factory=list)
    hints: list[dict[str, Any]] = field(default_factory=list)
    generated_at: str = ""
    report_id: str | None = None
    document_version: int | None = None
    paper_snapshot_hash: str | None = None
    quality_profile: str = "draft"
    review_style: str = "narrative"
    readiness_status: str = "unassessed"
    stale: bool = False
    blockers: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    core_claim_count: int = 0
    core_claim_fulltext_count: int = 0
    core_claim_fulltext_coverage: float = 0.0
    layout_checks: dict[str, Any] = field(default_factory=dict)
    claim_evidence: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def to_payload(self) -> dict[str, Any]:
        return {
            "section_count": self.section_count,
            "word_count": self.word_count,
            "cite_count": self.cite_count,
            "unique_cite_count": self.unique_cite_count,
            "whitelist_size": self.whitelist_size,
            "citation_density": round(self.citation_density, 4),
            "library_coverage": round(self.library_coverage, 4),
            "recent_ratio": round(self.recent_ratio, 4),
            "fulltext_coverage": round(self.fulltext_coverage, 4),
            "sections_without_citations": self.sections_without_citations,
            "soft_check": self.soft_check,
            "hints": self.hints,
            "generated_at": self.generated_at,
            "report_id": self.report_id,
            "document_version": self.document_version,
            "paper_snapshot_hash": self.paper_snapshot_hash,
            "quality_profile": self.quality_profile,
            "review_style": self.review_style,
            "readiness_status": self.readiness_status,
            "stale": self.stale,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "scores": self.scores,
            "core_claim_count": self.core_claim_count,
            "core_claim_fulltext_count": self.core_claim_fulltext_count,
            "core_claim_fulltext_coverage": round(self.core_claim_fulltext_coverage, 4),
            "layout_checks": self.layout_checks,
        }


_PLACEHOLDER_RE = re.compile(
    r"(?:待实验补充|待补充实验数据|待补充|TODO|TBD|PLACEHOLDER|\[待[^\]]*\])",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
_CHEMICAL_NAME_RE = re.compile(r"(?<!\w)[A-Za-z\u4e00-\u9fff]+-\d+-[A-Za-z\u4e00-\u9fff]+")
_IDENTIFIER_NAME_RE = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*[-_.]?\d+(?:\.\d+)*(?:[A-Za-z]+)?|v\d+(?:\.\d+)*)\b",
    re.IGNORECASE,
)
_CAUSAL_RE = re.compile(
    r"(?:导致|促进|抑制|引起|驱动|because|caus(?:e|es|ed|al)|lead(?:s|ing)? to|result(?:s|ed)? in)",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(
    r"(?:高于|低于|优于|相比|差异|more than|less than|higher|lower|superior|compared with)",
    re.IGNORECASE,
)
_EFFECT_RE = re.compile(
    r"(?:显著|效应|提高|降低|增加|减少|改善|损伤|significant|effect|improv|increase|decrease|reduce)",
    re.IGNORECASE,
)
_CONCLUSION_RE = re.compile(
    r"(?:表明|说明|证实|结论|因此|综上|demonstrat|indicat|suggest|conclude|therefore)",
    re.IGNORECASE,
)


def classify_claim(text: str) -> str:
    """将需要全文支持的论断分成可解释类别，并排除化学命名中的数字。"""
    number_text = _IDENTIFIER_NAME_RE.sub("", _CHEMICAL_NAME_RE.sub("", text))
    if _NUMBER_RE.search(number_text):
        return "numeric"
    if _CAUSAL_RE.search(text):
        return "causal"
    if _COMPARISON_RE.search(text):
        return "comparison"
    if _EFFECT_RE.search(text):
        return "effect"
    if _CONCLUSION_RE.search(text):
        return "conclusion"
    return "background"


def build_claim_evidence(
    *,
    rows: list[Any],
    evidence_sources: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 Paragraph/List IR 构造论断—证据矩阵，引用绑定到句级论断。"""
    anchors: list[dict[str, Any]] = []
    for row in rows:
        body = getattr(row, "body_ir_json", None) or {}
        for block in body.get("blocks") or []:
            containers = []
            if block.get("type") == "paragraph":
                containers = [block.get("runs") or []]
            elif block.get("type") == "list":
                containers = [item.get("runs") or [] for item in block.get("items") or []]
            for runs in containers:
                for sentence, cite_keys in _claims_with_local_citations(runs):
                    claim_kind = classify_claim(sentence)
                    is_core = claim_kind != "background"
                    for cite_key in cite_keys or [None]:
                        source = evidence_sources.get(cite_key or "", {})
                        point = _best_evidence_point(
                            source.get("quotable_points") or [],
                            claim=sentence,
                        )
                        excerpt = str(point.get("text") or "").strip() or None
                        fulltext = bool(source.get("fulltext_used"))
                        located = fulltext and bool(
                            point.get("page") is not None
                            or point.get("section")
                            or point.get("paragraph") is not None
                        )
                        score = _support_score(sentence, excerpt or "") if excerpt else None
                        supported = bool(located and score is not None and score >= 0.12)
                        if cite_key is None:
                            source_kind = "none"
                            support_status = "missing_citation" if is_core else "uncited_background"
                        elif not fulltext:
                            source_kind = "abstract"
                            support_status = "abstract_only" if is_core else "background_supported"
                        elif not located:
                            source_kind = "fulltext"
                            support_status = "fulltext_unlocated"
                        else:
                            source_kind = "fulltext"
                            support_status = "supported" if supported else "insufficient_support"
                        claim_hash = hashlib.sha256(sentence.encode()).hexdigest()
                        anchors.append(
                            {
                                "section_id": row.id,
                                "work_id": source.get("work_id"),
                                "section_key": row.section_key,
                                "claim_hash": claim_hash,
                                "claim_text": sentence,
                                "claim_kind": claim_kind,
                                "is_core": is_core,
                                "cite_key": cite_key,
                                "source_kind": source_kind,
                                "source_page": point.get("page"),
                                "source_section": point.get("section"),
                                "source_paragraph": point.get("paragraph"),
                                "evidence_excerpt": excerpt,
                                "evidence_hash": (
                                    hashlib.sha256(excerpt.encode()).hexdigest()
                                    if excerpt
                                    else None
                                ),
                                "support_status": support_status,
                                "support_score": score,
                                "manual_status": "unreviewed",
                            }
                        )
    return anchors


def _claims_with_local_citations(runs: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    """Bind CiteRuns to the nearest sentence instead of every claim in the paragraph."""
    claims: list[dict[str, Any]] = []
    buffer = ""
    pending_keys: list[str] = []

    def append_claim(value: str) -> None:
        text = " ".join(value.split()).strip()
        if len(text) >= 12:
            claims.append({"text": text, "cite_keys": list(dict.fromkeys(pending_keys))})
        pending_keys.clear()

    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("t") == "text":
            value = str(run.get("v") or "")
            buffer = f"{buffer} {value}".strip()
            while True:
                match = re.search(r"[。！？!?]|(?<!\d)\.(?!\d)", buffer)
                if match is None:
                    break
                end = match.end()
                append_claim(buffer[:end])
                buffer = buffer[end:].strip()
        elif run.get("t") == "cite":
            keys = [str(key) for key in run.get("keys") or []]
            if buffer.strip():
                pending_keys.extend(keys)
            elif claims:
                claims[-1]["cite_keys"] = list(dict.fromkeys([*claims[-1]["cite_keys"], *keys]))
            else:
                pending_keys.extend(keys)
    if buffer.strip():
        append_claim(buffer)
    elif pending_keys and claims:
        claims[-1]["cite_keys"] = list(dict.fromkeys([*claims[-1]["cite_keys"], *pending_keys]))
    return [(str(item["text"]), list(item["cite_keys"])) for item in claims]


def apply_readiness_gate(
    report: QualityReport,
    *,
    rows: list[Any],
    project: Any,
    whitelist: set[str],
    search_runs: list[Any],
) -> QualityReport:
    """应用双模式质量门；分项评分不参与“堆数量过线”。"""
    all_text = " ".join(_body_text(getattr(row, "body_ir_json", None) or {}) for row in rows)
    unresolved = sorted(
        {
            key
            for row in rows
            for key in (getattr(row, "cite_keys_json", None) or [])
            if key not in whitelist
        }
    )
    citation_warnings = sum(
        len((getattr(row, "body_ir_json", None) or {}).get("citation_warnings") or [])
        for row in rows
    )
    core = [anchor for anchor in report.claim_evidence if anchor["is_core"]]
    supported_hashes = {
        anchor["claim_hash"]
        for anchor in core
        if anchor["source_kind"] == "fulltext"
        and anchor.get("manual_status") != "rejected"
        and (
            anchor["support_status"] == "supported"
            or (
                anchor.get("manual_status") == "confirmed"
                and (
                    anchor.get("source_page") is not None
                    or anchor.get("source_section")
                    or anchor.get("source_paragraph") is not None
                )
            )
        )
    }
    core_hashes = {anchor["claim_hash"] for anchor in core}
    report.core_claim_count = len(core_hashes)
    report.core_claim_fulltext_count = len(supported_hashes)
    report.core_claim_fulltext_coverage = (
        len(supported_hashes) / len(core_hashes) if core_hashes else 1.0
    )

    blockers: list[dict[str, Any]] = []
    if _PLACEHOLDER_RE.search(all_text):
        blockers.append(_issue("placeholders_present", "正文仍含待补占位符"))
    missing_evidence = len(core_hashes - supported_hashes)
    if missing_evidence:
        blockers.append(
            _issue(
                "core_claim_fulltext_missing",
                f"{missing_evidence} 条核心论断没有可定位且相符的全文证据",
                count=missing_evidence,
            )
        )
    if unresolved or citation_warnings:
        blockers.append(
            _issue(
                "citation_resolution_failed",
                "存在未解析或已被移除的引用",
                cite_keys=unresolved,
                warning_count=citation_warnings,
            )
        )
    unapproved = [row.section_key for row in rows if getattr(row, "status", "") != "approved"]
    if unapproved:
        blockers.append(
            _issue(
                "sections_unapproved",
                f"{len(unapproved)} 个章节尚未审批",
                section_keys=unapproved,
            )
        )
    publication_title = str(getattr(project, "publication_title", None) or "").strip()
    if not publication_title:
        blockers.append(_issue("publication_title_missing", "尚未确认论文发表题名"))
    language = getattr(project, "language", None)
    has_cjk_title = bool(re.search(r"[一-鿿]", publication_title))
    if publication_title and (
        (language == "en" and has_cjk_title) or (language == "zh" and not has_cjk_title)
    ):
        blockers.append(_issue("metadata_script_mismatch", "发表题名与论文语言脚本不一致"))
    if not (getattr(project, "authors_json", None) or []):
        blockers.append(_issue("authors_missing", "尚未填写作者"))
    if not (getattr(project, "keywords_json", None) or []):
        blockers.append(_issue("keywords_missing", "尚未填写关键词"))
    if getattr(project, "metadata_confirmed_at", None) is None:
        blockers.append(_issue("metadata_unconfirmed", "题名、作者、关键词和语言脚本尚未确认"))
    if report.review_style == "systematic":
        completed = [run for run in search_runs if getattr(run, "status", None) == "succeeded"]
        if not completed or any(getattr(run, "error", None) for run in search_runs):
            blockers.append(
                _issue(
                    "systematic_search_incomplete",
                    "系统综述需要完整、无失败的真实检索日志",
                )
            )

    warnings = [
        (
            dict(hint)
            if hint.get("code")
            else {
                "code": str(hint.get("kind") or hint.get("reason") or "quality_hint"),
                "message": str(hint.get("message") or "论文质量提示"),
                **hint,
            }
        )
        for hint in report.hints
    ]
    degraded_searches = [
        run
        for run in search_runs
        if getattr(run, "status", None) != "succeeded" or getattr(run, "error", None)
    ]
    if degraded_searches:
        warnings.append(
            _issue(
                "search_degraded",
                f"{len(degraded_searches)} 次学术检索失败或降级，覆盖范围可能不完整",
                providers=sorted(
                    {str(getattr(run, "provider", None) or "unknown") for run in degraded_searches}
                ),
            )
        )
    if report.word_count < 3000:
        warnings.append(_issue("short_manuscript", "正文篇幅低于 3000 字词单位"))
    if report.fulltext_coverage < 0.5:
        warnings.append(_issue("low_fulltext_coverage", "入选文献全文卡片覆盖率低于 50%"))
    if report.recent_ratio > 0.85 and report.whitelist_size >= 10:
        warnings.append(_issue("year_imbalance", "文献过度集中于近五年，需补充基础研究"))
    if getattr(project, "paper_type", None) == "review":
        section_titles = " ".join(str(getattr(row, "title", "")) for row in rows).lower()
        synthesis_terms = (
            "跨研究",
            "跨文献",
            "证据冲突",
            "局限",
            "cross-study",
            "comparison",
            "evidence conflict",
            "limitations",
        )
        if not any(term in section_titles for term in synthesis_terms):
            warnings.append(
                _issue(
                    "review_synthesis_missing",
                    "综述缺少跨文献比较、方法局限或证据冲突综合章节",
                )
            )

    report.scores = {
        "evidence": round(report.core_claim_fulltext_coverage * 100, 1),
        "citation": round(min(100.0, report.citation_density / 8 * 100), 1),
        "literature_balance": round(max(0.0, 100 - abs(report.recent_ratio - 0.6) * 125), 1),
        "completeness": round((1 - len(unapproved) / max(1, len(rows))) * 100, 1),
        "metadata": (
            100.0
            if not any(
                item["code"].startswith(
                    ("publication_title_", "authors_", "keywords_", "metadata_")
                )
                for item in blockers
            )
            else 0.0
        ),
        "layout": 100.0 if report.layout_checks.get("passed") is True else 0.0,
    }
    report.blockers = blockers if report.quality_profile == "submission" else []
    report.warnings = warnings + (blockers if report.quality_profile == "draft" else [])
    report.readiness_status = (
        "preflight_ready"
        if report.quality_profile == "submission" and not blockers
        else ("needs_revision" if report.quality_profile == "submission" else "draft")
    )
    return report


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _body_text(body: dict[str, Any]) -> str:
    return " ".join(
        str(run.get("v") or "")
        for block in body.get("blocks") or []
        for runs in (
            [block.get("runs") or []]
            if block.get("type") == "paragraph"
            else [item.get("runs") or [] for item in block.get("items") or []]
        )
        for run in runs
        if run.get("t") == "text"
    )


def _split_claim_sentences(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"(?<=[。！？.!?])\s*", " ".join(text.split()))
        if len(item.strip()) >= 12
    ]


def _best_evidence_point(points: list[Any], *, claim: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for point in points:
        if isinstance(point, dict) and point.get("text"):
            candidates.append(point)
        elif isinstance(point, str) and point.strip():
            candidates.append({"text": point.strip()})
    return max(
        candidates,
        key=lambda point: (
            _support_score(claim, str(point.get("text") or "")),
            int(
                point.get("page") is not None
                or bool(point.get("section"))
                or point.get("paragraph") is not None
            ),
        ),
        default={},
    )


def _support_score(claim: str, evidence: str) -> float:
    def tokens(value: str) -> set[str]:
        lowered = value.lower()
        return set(re.findall(r"[a-z][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", lowered))

    claim_tokens = tokens(claim)
    evidence_tokens = tokens(evidence)
    return len(claim_tokens & evidence_tokens) / max(1, len(claim_tokens))


async def soft_check_citations(
    *,
    usages: list[dict[str, Any]],
    abstracts: dict[str, str],
    runner: LLMRunner | None,
    limit: int = MAX_SOFT_CHECKS,
) -> list[SoftCheckFinding]:
    """对引用位置做语义相关性软校验（verifier 角色，便宜档）。

    只产出提示：低分在编辑器里显示黄色徽章，**不删除引用、不阻断渲染**
    （设计 §4.4.3：执行点从「渲染前拒绝」移到「编辑器内提示」）。
    """
    checkable = [
        usage
        for usage in usages
        if usage.get("cite_key") in abstracts and usage.get("context_snippet")
    ][:limit]
    if not checkable or runner is None or not runner.enabled:
        return []

    lines = []
    for index, usage in enumerate(checkable):
        key = str(usage["cite_key"])
        lines.append(
            f"[{index}] 上下文: {str(usage['context_snippet'])[:300]}\n"
            f"     被引文献({key})摘要: {abstracts[key][:400]}"
        )
    result = await runner.agenerate_json(
        "verifier",
        system_prompt=_SOFT_CHECK_PROMPT,
        user_prompt="\n\n".join(lines),
        max_output_tokens=2000,
        temperature=0.0,
        metadata={"stage": "soft_check"},
    )
    if not result.ok or not isinstance(result.value, dict):
        return []

    findings: list[SoftCheckFinding] = []
    for item in result.value.get("judgements") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or not 0 <= index < len(checkable):
            continue
        raw_score = item.get("score")
        score = float(raw_score) if isinstance(raw_score, int | float) else 0.5
        usage = checkable[index]
        findings.append(
            SoftCheckFinding(
                cite_key=str(usage["cite_key"]),
                section_key=str(usage.get("section_key") or ""),
                score=min(1.0, max(0.0, score)),
                reason=str(item.get("reason") or "")[:200],
                context=str(usage.get("context_snippet") or ""),
            )
        )
    return findings


def coverage_hints(
    *,
    sections: list[dict[str, Any]],
    whitelist_size: int,
    used_keys: set[str],
    publication_years: list[int],
    scope: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """覆盖建议器（由 gap_analysis 降级而来，设计 §3.2）：只提示，绝不阻断。"""
    clock = now or datetime.now(UTC)
    hints: list[dict[str, Any]] = []

    for section in sections:
        key = str(section.get("section_key") or "")
        words = int(section.get("word_count") or 0)
        cites = len(section.get("cite_keys") or [])
        if section.get("kind") == "frame":
            continue
        if words >= 300 and cites == 0:
            hints.append(
                {
                    "kind": "no_citations",
                    "section_key": key,
                    "message": f"「{section.get('title') or key}」有 {words} 字但没有任何引用",
                }
            )
        elif words >= 800 and cites <= 1:
            title = section.get("title") or key
            hints.append(
                {
                    "kind": "low_citation_density",
                    "section_key": key,
                    "message": f"「{title}」引用密度偏低（{cites} 条 / {words} 字）",
                }
            )

    unused = whitelist_size - len(used_keys)
    if whitelist_size and unused > whitelist_size * 0.3:
        hints.append(
            {
                "kind": "unused_library",
                "message": f"入库文献有 {unused} 篇未被正文引用，考虑补充论述或从库中移除",
            }
        )

    recent = [year for year in publication_years if year >= clock.year - RECENT_YEARS_WINDOW]
    if publication_years and len(recent) / len(publication_years) < 0.3:
        hints.append(
            {
                "kind": "stale_library",
                "message": (
                    f"近 {RECENT_YEARS_WINDOW} 年文献仅占 "
                    f"{len(recent)}/{len(publication_years)}，建议补充近期工作"
                ),
            }
        )

    section_titles = " ".join(str(s.get("title") or "") for s in sections).lower()
    topic = str((scope or {}).get("topic") or "").lower()
    for subtopic in (scope or {}).get("subtopics") or []:
        text = str(subtopic).strip()
        # 确定性回退的 subtopics 就是主题切词，逐词提示只会刷屏；
        # 只对「与主题不同的、实义的」子主题给覆盖建议。
        if len(text) < 4 or text.lower() in topic:
            continue
        if text.lower() not in section_titles:
            hints.append(
                {
                    "kind": "uncovered_subtopic",
                    "message": f"SCOPE 中的子主题「{text}」在章节标题里没有对应位置",
                }
            )
    return hints


def build_quality_report(
    *,
    sections: list[dict[str, Any]],
    whitelist_size: int,
    publication_years: list[int],
    fulltext_coverage: float = 0.0,
    soft_check: list[SoftCheckFinding] | None = None,
    scope: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> QualityReport:
    """质量评分报告：只呈现，不设门槛（取代 formal completion 的 12 项硬门槛）。"""
    clock = now or datetime.now(UTC)
    word_count = sum(int(s.get("word_count") or 0) for s in sections)
    used_keys: set[str] = set()
    cite_count = 0
    for section in sections:
        keys = section.get("cite_keys") or []
        cite_count += len(keys)
        used_keys.update(keys)

    recent = [year for year in publication_years if year >= clock.year - RECENT_YEARS_WINDOW]
    report = QualityReport(
        section_count=len(sections),
        word_count=word_count,
        cite_count=cite_count,
        unique_cite_count=len(used_keys),
        whitelist_size=whitelist_size,
        # 每千字引用数：综述通常 8–20 之间。
        citation_density=(cite_count / word_count * 1000) if word_count else 0.0,
        library_coverage=(len(used_keys) / whitelist_size) if whitelist_size else 0.0,
        recent_ratio=(len(recent) / len(publication_years)) if publication_years else 0.0,
        fulltext_coverage=fulltext_coverage,
        sections_without_citations=[
            str(s.get("section_key"))
            for s in sections
            if s.get("kind") != "frame" and not (s.get("cite_keys") or [])
        ],
        soft_check=[f.to_payload() for f in (soft_check or []) if f.weak],
        generated_at=clock.isoformat(),
    )
    report.hints = coverage_hints(
        sections=sections,
        whitelist_size=whitelist_size,
        used_keys=used_keys,
        publication_years=publication_years,
        scope=scope,
        now=clock,
    )
    return report


def count_words(text: str) -> int:
    cjk = len(re.findall(r"[一-鿿]", text or ""))
    latin = len(re.findall(r"[A-Za-z][A-Za-z'-]*", text or ""))
    return cjk + latin


__all__ = [
    "MAX_SOFT_CHECKS",
    "SOFT_CHECK_THRESHOLD",
    "QualityReport",
    "SoftCheckFinding",
    "apply_readiness_gate",
    "build_claim_evidence",
    "build_quality_report",
    "classify_claim",
    "count_words",
    "coverage_hints",
    "soft_check_citations",
]
