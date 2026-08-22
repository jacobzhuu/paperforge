"""Phase 4：子问题内的叙述性跨研究综合（P0-4）。

确定性 SYNTH 只会数立场：同一 ``comparability_key`` 下同时出现 supports 与
contradicts 就叫 conflicting，否则 consistent。它答不出「在什么条件下成立、为什么
不一致」，于是正文只能罗列文献。本模块补上那段推理。

设计红线：**只增补，不夺权**。模型在这里不能改 ``answer_status``、不能改证据归属、
不能改 ``comparison_clusters``、更不能写引用。它唯一能产出的是一段带 EVIDENCE_ID 的
叙述，而且每一条都要过下面五道硬过滤（照抄 ``qmatrix._normalize_links`` 的纪律：
不合规就丢，不做「宽容修补」）：

1. 引用的 ``evidence_id`` 必须在 bundle 内；剩下零个引用的条目整条丢弃。
2. ``conflict`` 必须落在 bundle 已有的某个可比簇内，且跨 ≥2 个 work——
   不可比的两项研究之间不存在"冲突"这回事。
3. ``conditional`` 的维度必须来自任务本体的 ``dimension_schema_json`` 或核心维度集。
4. 只引用了 ``D_abstract_only`` 的条目降级成 gap 并标注出处限制。
5. 语句里出现、但在被引证据中找不到出处的数字 → 整条丢弃；``claim`` 违规则置空。

第 5 条直接复用 NUMLINT（``build_asset_index`` + ``lint_text``），与导出前的数值
红线是同一把尺子——年份、图表序号、版本号这些非实验数值也因此自动豁免。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ingest.numlint import build_asset_index, lint_text
from llm_runtime import LLMRunner

from paperforge_worker.locators import locator_display

SYNTHESIZER_ROLE = "synthesizer"

# 全文级证据。D_abstract_only 只能用于背景或「该文献报告」式转述，不能承载综合结论。
FULLTEXT_GRADES = frozenset({"A_located_structured", "B_located_prose", "C_fulltext_unlocated"})

# 任务本体没有声明维度时的兜底集合。与 comparability_key 的构成维度对齐。
CORE_DIMENSIONS = frozenset(
    {"dataset", "split", "task", "metric_name", "model_family", "sample_size"}
)

MAX_ENTRIES_PER_KIND = 6
MAX_OUTPUT_TOKENS = 6000
MAX_RAW_ENTRIES = 24
MIN_ELIGIBLE_UNITS = 2
MAX_STATEMENT_CHARS = 400
SYNTHESIS_CONTEXT_CHAR_BUDGET = 24_000
EVIDENCE_TEXT_CHARS = 900

_ENTRY_KINDS = ("agreement", "conditional", "conflict")


@dataclass(frozen=True)
class SynthesisEntry:
    kind: str
    statement: str
    evidence_ids: tuple[str, ...] = ()
    dimension: str | None = None
    comparability_key: str | None = None
    demoted_from: str | None = None
    note: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"statement": self.statement}
        if self.evidence_ids:
            payload["evidence_ids"] = list(self.evidence_ids)
        if self.dimension:
            payload["dimension"] = self.dimension
        if self.comparability_key:
            payload["comparability_key"] = self.comparability_key
        if self.demoted_from:
            payload["demoted_from"] = self.demoted_from
        if self.note:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True)
class QuestionSynthesisResult:
    generator: str
    claim: str | None = None
    agreement: tuple[SynthesisEntry, ...] = ()
    conditional: tuple[SynthesisEntry, ...] = ()
    conflict: tuple[SynthesisEntry, ...] = ()
    gap: tuple[SynthesisEntry, ...] = ()
    rejected: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """一条都没留下。此时仍要落行（generator='deterministic'）当作负缓存。"""
        return not (
            self.claim or self.agreement or self.conditional or self.conflict or self.gap
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "generator": self.generator,
            "claim": self.claim,
            "agreement": [entry.to_payload() for entry in self.agreement],
            "conditional": [entry.to_payload() for entry in self.conditional],
            "conflict": [entry.to_payload() for entry in self.conflict],
            "gap": [entry.to_payload() for entry in self.gap],
        }


def bundle_fingerprint(bundle: dict[str, Any]) -> str:
    """确定性 bundle 的指纹；``synthesis`` 键本身不参与，否则永远无法命中。"""
    material = {key: value for key, value in bundle.items() if key != "synthesis"}
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def eligible_unit_count(bundle: dict[str, Any]) -> int:
    return sum(
        1
        for row in bundle.get("evidence") or []
        if str(row.get("grade") or "") in FULLTEXT_GRADES
    )


def should_synthesize(bundle: dict[str, Any]) -> bool:
    """要不要为这个 bundle 花一次调用。

    证据不足的子问题本来就已经被标成缺口，让模型去"综合"零到一条证据只会诱导它
    补全——这正是最贵也最危险的一次调用。
    """
    if str(bundle.get("answer_status") or "") == "insufficient_evidence":
        return False
    return eligible_unit_count(bundle) >= MIN_ELIGIBLE_UNITS


async def synthesize_bundle(
    *,
    bundle: dict[str, Any],
    runner: LLMRunner | None,
    allowed_dimensions: frozenset[str],
    language: str = "en",
) -> QuestionSynthesisResult | None:
    """跑一次综合；runner 不可用或响应不是 JSON 对象时返回 ``None``（调用方保持确定性行为）。"""
    if runner is None or not runner.enabled:
        return None
    result = await runner.agenerate_json(
        SYNTHESIZER_ROLE,
        system_prompt=_system_prompt(language=language, dimensions=allowed_dimensions),
        user_prompt=render_bundle(bundle),
        # 综合本身只输出几百 token，但这是个推理档位的角色（synthesizer → planner），
        # 思考 token 同样计入输出预算。影子评估实测：1600 会让 6 个子问题中的 4 个被
        # `output_truncated` 打掉（最坏的一种结果：钱花了，一条都没拿到）；成功调用的
        # 平均输出是 3663 token，4000 仍有 24% 的调用要重试。
        max_output_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.0,
        metadata={"stage": "synthesis", "question_id": str(bundle.get("question_id") or "")},
    )
    if not result.ok or not isinstance(result.value, dict):
        return None
    return build_synthesis(
        payload=result.value,
        bundle=bundle,
        allowed_dimensions=allowed_dimensions,
        model=result.model,
    )


def build_synthesis(
    *,
    payload: dict[str, Any],
    bundle: dict[str, Any],
    allowed_dimensions: frozenset[str],
    model: str | None = None,
) -> QuestionSynthesisResult:
    """把一份模型输出过一遍五道准入规则。纯函数，便于直接断言。"""
    rows = {
        str(row.get("evidence_id")): row
        for row in bundle.get("evidence") or []
        if row.get("evidence_id")
    }
    clusters = {
        str(cluster.get("comparability_key")): {
            str(value) for value in cluster.get("evidence_ids") or []
        }
        for cluster in bundle.get("comparison_clusters") or []
    }
    dimensions = allowed_dimensions or frozenset(CORE_DIMENSIONS)

    kept: dict[str, list[SynthesisEntry]] = {kind: [] for kind in _ENTRY_KINDS}
    gaps: list[SynthesisEntry] = []
    rejected: list[dict[str, Any]] = []

    for kind in _ENTRY_KINDS:
        raw_entries = payload.get(kind)
        # 上限落在**读入的条数**上，不只是留下的条数：降级成 gap 的条目不计入
        # kept，否则一份超长响应仍会被整份走完。
        for item in (raw_entries if isinstance(raw_entries, list) else [])[:MAX_RAW_ENTRIES]:
            if len(kept[kind]) >= MAX_ENTRIES_PER_KIND:
                break
            entry, reason = _accept_entry(
                item,
                kind=kind,
                rows=rows,
                clusters=clusters,
                dimensions=dimensions,
            )
            if entry is None:
                rejected.append(
                    {
                        "kind": kind,
                        "reason": reason,
                        "statement": _clean(item)[:160],
                        # 被拒的那个值本身。只记"维度不认识"无法判断该扩本体还是该改
                        # 提示词——影子评估正是靠这一条定位本体缺口。
                        "value": _rejected_value(item, reason),
                    }
                )
                continue
            if entry.kind == "gap":
                if len(gaps) < MAX_ENTRIES_PER_KIND:
                    gaps.append(entry)
            else:
                kept[kind].append(entry)

    raw_gaps = payload.get("gap")
    for item in (raw_gaps if isinstance(raw_gaps, list) else [])[:MAX_RAW_ENTRIES]:
        if len(gaps) >= MAX_ENTRIES_PER_KIND:
            break
        statement = _statement(item)
        if not statement:
            rejected.append({"kind": "gap", "reason": "empty_statement", "statement": ""})
            continue
        gaps.append(SynthesisEntry(kind="gap", statement=statement))

    claim = _statement(payload.get("claim"))
    if claim and _unsourced_numbers(claim, rows.values()):
        rejected.append({"kind": "claim", "reason": "unsourced_number", "statement": claim[:160]})
        claim = ""

    return QuestionSynthesisResult(
        generator=f"llm:{model}" if model else "llm:unknown",
        claim=claim or None,
        agreement=tuple(kept["agreement"]),
        conditional=tuple(kept["conditional"]),
        conflict=tuple(kept["conflict"]),
        gap=tuple(gaps),
        rejected=tuple(rejected),
    )


def _rejected_value(item: Any, reason: str) -> str:
    if not isinstance(item, dict):
        return ""
    if reason == "unknown_dimension":
        return _clean(item.get("dimension"))[:64]
    if reason in {"unknown_comparability_key", "evidence_outside_cluster"}:
        return _clean(item.get("comparability_key"))[:64]
    return ""


def _accept_entry(
    item: Any,
    *,
    kind: str,
    rows: dict[str, dict[str, Any]],
    clusters: dict[str, set[str]],
    dimensions: frozenset[str],
) -> tuple[SynthesisEntry | None, str]:
    if not isinstance(item, dict):
        return None, "not_an_object"
    statement = _statement(item.get("statement"))
    if not statement:
        return None, "empty_statement"

    # 规则 1：越界 id 丢弃；一个都不剩的条目整条丢弃。
    cited = [
        str(value)
        for value in dict.fromkeys(
            str(raw) for raw in item.get("evidence_ids") or [] if isinstance(raw, (str, int))
        )
        if str(value) in rows
    ]
    if not cited:
        return None, "no_bundle_evidence"
    units = [rows[evidence_id] for evidence_id in cited]

    # 规则 4：只有摘要级证据 → 降级成 gap 并保留出处限制说明。
    if all(str(unit.get("grade") or "") not in FULLTEXT_GRADES for unit in units):
        return (
            SynthesisEntry(
                kind="gap",
                statement=statement,
                evidence_ids=tuple(cited),
                demoted_from=kind,
                note="abstract-only evidence: attribution or background use only",
            ),
            "",
        )

    dimension: str | None = None
    comparability_key: str | None = None

    # 规则 3：条件差异的维度必须来自任务本体或核心维度集。
    if kind == "conditional":
        dimension = _clean(item.get("dimension")).casefold()
        if dimension not in dimensions:
            return None, "unknown_dimension"

    # 规则 2：冲突必须落在同一个可比簇内，且跨 ≥2 个 work。
    if kind == "conflict":
        comparability_key = _clean(item.get("comparability_key"))
        cluster = clusters.get(comparability_key)
        if cluster is None:
            return None, "unknown_comparability_key"
        if not set(cited) <= cluster:
            return None, "evidence_outside_cluster"
        if len({str(unit.get("work_id") or "") for unit in units}) < 2:
            return None, "single_work_conflict"

    # 规则 5：数字必须在被引证据里找得到出处。
    if _unsourced_numbers(statement, units):
        return None, "unsourced_number"

    return (
        SynthesisEntry(
            kind=kind,
            statement=statement,
            evidence_ids=tuple(cited),
            dimension=dimension or None,
            comparability_key=comparability_key or None,
        ),
        "",
    )


def _unsourced_numbers(statement: str, units: Any) -> list[str]:
    """语句里找不到出处的实验数值。空列表表示通过。"""
    index = build_asset_index([], [_numeric_source(unit) for unit in units])
    findings = lint_text(statement, section_key="synthesis", asset_index=index)
    return [finding.value for finding in findings if finding.status == "unsourced"]


def _numeric_source(unit: dict[str, Any]) -> dict[str, Any]:
    """证据单元 → NUMLINT 的文献来源条目。测量值一并展开，表格里的数字才算有出处。"""
    measurements = " ".join(
        f"{row.get('metric_name')} {row.get('value')} {row.get('sample_size') or ''}"
        for row in unit.get("measurements") or []
    )
    return {
        "located": True,
        "cite_key": unit.get("cite_key") or unit.get("evidence_id"),
        "text": f"{unit.get('text') or ''} {measurements}",
    }


def render_bundle(bundle: dict[str, Any]) -> str:
    """把确定性 bundle 渲染成模型输入。可比簇是这里最重要的结构信号。"""
    lines = [
        f"SUB-QUESTION: {bundle.get('question')}",
        f"DETERMINISTIC STATUS: {bundle.get('answer_status')} "
        f"(stance={bundle.get('stance_summary')})",
    ]
    dimensions = bundle.get("comparison_dimensions") or []
    if dimensions:
        lines.append(f"QUESTION COMPARISON DIMENSIONS: {', '.join(str(v) for v in dimensions)}")
    for cluster in bundle.get("comparison_clusters") or []:
        lines.append(
            f"[COMPARABLE CLUSTER comparability_key={cluster.get('comparability_key')}] "
            f"classification={cluster.get('classification')} "
            f"evidence_ids={','.join(str(v) for v in cluster.get('evidence_ids') or [])}"
        )
    for group in bundle.get("not_comparable_groups") or []:
        lines.append(
            f"[NOT COMPARABLE comparability_key={group.get('comparability_key')}] "
            f"evidence_ids={','.join(str(v) for v in group.get('evidence_ids') or [])}"
        )

    used = len("\n".join(lines))
    for row in bundle.get("evidence") or []:
        locator = locator_display(row) or "unlocated"
        measurements = "; ".join(
            f"{item.get('metric_name')}={item.get('value')}{item.get('unit') or ''} "
            f"dataset={item.get('dataset') or 'unknown'} "
            f"comparability_key={item.get('comparability_key')}"
            for item in row.get("measurements") or []
        )
        rendered = (
            f"EVIDENCE_ID={row.get('evidence_id')} work_id={row.get('work_id')} "
            f"cite_key={row.get('cite_key')} grade={row.get('grade')} "
            f"stance={row.get('stance')} condition={row.get('condition_note') or 'none'}\n"
            f"  locator={locator}\n"
            f"  text={str(row.get('text') or '')[:EVIDENCE_TEXT_CHARS]}\n"
            f"  measurements={measurements or '(none)'}"
        )
        if used + len(rendered) > SYNTHESIS_CONTEXT_CHAR_BUDGET:
            break
        lines.append(rendered)
        used += len(rendered)
    return "\n".join(lines)


def _system_prompt(*, language: str, dimensions: frozenset[str]) -> str:
    target = "Simplified Chinese" if language == "zh" else "English"
    allowed = ", ".join(sorted(dimensions or CORE_DIMENSIONS))
    return f"""You synthesize evidence across studies for one sub-question of a literature review.

Write every `claim` and `statement` in {target}. Return STRICT JSON only:

{{"claim": "one-sentence answer, or null if the evidence cannot answer it",
  "agreement":   [{{"statement": "...", "evidence_ids": ["..."]}}],
  "conditional": [{{"dimension": "...", "statement": "...", "evidence_ids": ["..."]}}],
  "conflict":    [{{"statement": "...", "evidence_ids": ["..."], "comparability_key": "..."}}],
  "gap":         [{{"statement": "..."}}]}}

Hard rules — violations are discarded, so a smaller honest answer beats a fuller one:

1. Cite only EVIDENCE_ID values that appear in the input. Never invent an id.
2. A `conflict` may only be asserted inside one COMPARABLE CLUSTER: put that cluster's
   comparability_key in the entry, cite only evidence_ids listed in that cluster, and cite
   at least two different work_id values. Studies in different clusters measured different
   things; they cannot disagree.
3. `dimension` must be exactly one of: {allowed}.
4. Evidence marked grade=D_abstract_only cannot support a synthesis conclusion. If that is
   all you have for a point, report it under `gap` instead.
5. Every number you write must appear verbatim in the text or measurements of an evidence
   unit you cite. Do not compute, round, average, or convert. If you cannot quote a number,
   describe the direction in words.
6. Do not write citation markers, reference entries, author names, or years. Binding is
   done downstream from evidence_ids.

Use `gap` to state what the evidence does not settle. An empty list is a valid answer."""


# 「Qin等人（2025）」这类署名归属。提示词第 6 条禁止它，但提示词不是防线：
# 影子评估里 92 条通过校验的条目仍出现了一条。综合语句是给写作器的论证指引，
# 里面的作者—年份既没有绑定也没有白名单，删掉比留着安全（与 `writing._clean_paragraph`
# 对正文的处理一致）。删的是署名，不是内容——语句本身仍由 evidence_ids 承担出处。
_ATTRIBUTION_RE = re.compile(
    r"(?:[A-Z][A-Za-z\-]+|[\u4e00-\u9fff]{1,4})\s*(?:et\s+al\.|等人|等)?\s*"
    r"[（(]\s*(?:19|20)\d{2}[a-z]?\s*[)）]"
    r"|[（(]\s*(?:19|20)\d{2}[a-z]?\s*[)）]"
)


def _statement(value: Any) -> str:
    cleaned = _ATTRIBUTION_RE.sub("", _clean(value))
    return " ".join(cleaned.split())[:MAX_STATEMENT_CHARS]


def _clean(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("statement")
    return " ".join(value.split()) if isinstance(value, str) else ""


__all__ = [
    "CORE_DIMENSIONS",
    "MAX_OUTPUT_TOKENS",
    "FULLTEXT_GRADES",
    "MAX_ENTRIES_PER_KIND",
    "MIN_ELIGIBLE_UNITS",
    "SYNTHESIZER_ROLE",
    "QuestionSynthesisResult",
    "SynthesisEntry",
    "build_synthesis",
    "bundle_fingerprint",
    "eligible_unit_count",
    "render_bundle",
    "should_synthesize",
    "synthesize_bundle",
]
