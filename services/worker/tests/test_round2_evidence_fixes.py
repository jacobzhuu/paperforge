"""Regression coverage for evidence_pipeline_round2_review_and_fix_plan.md."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from db.repositories.tasks import TaskSpec, infer_task_id, topical_status
from db.text_safety import sanitize_pg_text
from paperforge_worker.pipelines.evidence import _fulltext_grade
from paperforge_worker.pipelines.qmatrix import (
    _degraded_candidates,
    _diverse_ranked_candidates,
    _question_terms,
    _rank_candidates,
)
from paperforge_worker.pipelines.scope import normalize_scope, search_queries
from paperforge_worker.pipelines.screen import _phrase_in_text, screen_eligibility
from paperforge_worker.pipelines.writing import (
    SectionDraft,
    WritingContext,
    enforce_sentence_evidence_rules,
    write_section,
)


def _unit(*, text: str, grade: str = "B_located_prose", kind: str = "experimental_fact"):
    return SimpleNamespace(
        id=uuid4(),
        work_id=uuid4(),
        text=text,
        section_path="Results",
        grade=grade,
        kind=kind,
        page=3,
        paragraph_index=1,
        object_ref=None,
        task_id=None,
    )


def test_qmatrix_cross_language_uses_comparison_dimensions() -> None:
    """N0-1: Chinese question + English evidence must not zero out when dims bridge."""
    question = SimpleNamespace(
        text="评估序列推荐攻击常用的数据集和评价指标是什么？",
        comparison_dimensions_json=["MovieLens", "NDCG", "HR", "攻击预算"],
        search_query=None,
        term_aliases_json=None,
        task_id=None,
        expected_evidence_kinds_json=["experimental_fact"],
    )
    relevant = _unit(text="Results on MovieLens show poisoning reduces HR@20 and NDCG@10.")
    ranked = _rank_candidates(
        question,
        [relevant],
        expected_kinds={"experimental_fact"},
    )
    assert ranked
    assert ranked[0][0] is relevant


def test_qmatrix_zero_candidate_fallback_marks_degraded_mode() -> None:
    unrelated = _unit(text="An unrelated RNA folding model reaches AUROC 0.99.")
    question = SimpleNamespace(
        text="序列推荐攻击研究存在哪些不足与未来方向？",
        comparison_dimensions_json=[],
        search_query=None,
        term_aliases_json=None,
        task_id=None,
    )
    ranked, _rejected_task, rejected_lex, _bridge = _rank_candidates(
        question,
        [unrelated],
        expected_kinds={"experimental_fact"},
        return_diagnostics=True,
    )
    assert ranked == []
    assert rejected_lex >= 1
    degraded = _degraded_candidates([unrelated], task_id=None)
    assert degraded
    assert degraded[0][0] is unrelated


def test_sanitize_pg_text_strips_nul_and_controls() -> None:
    assert sanitize_pg_text("hello\x00world\x07!") == "helloworld!"
    assert sanitize_pg_text("\x00\x00") is None
    assert sanitize_pg_text(None) is None


def test_grade_requires_structured_cell_for_a() -> None:
    assert _fulltext_grade(1, "Results", 1, "fig:2", "object_mention") == "B_located_prose"
    assert _fulltext_grade(1, "Results", 1, "table:3", "structured_cell") == "A_located_structured"
    assert _fulltext_grade(None, None, None, None, "prose_only") == "C_fulltext_unlocated"


def test_question_terms_prefer_search_query_bridge() -> None:
    terms, source = _question_terms(
        "主要攻击方法有哪些？",
        dimensions=[],
        search_query="sequential recommendation poisoning attack",
        aliases=[],
    )
    assert source == "search_query"
    assert "poisoning" in terms
    assert "recommendation" in terms


def test_qmatrix_search_query_survives_chinese_dimensions() -> None:
    """Regression: non-empty Chinese dimensions must not hide the English bridge."""
    question = SimpleNamespace(
        text="目前提出了哪些防御策略来应对序列推荐系统中的攻击？",
        comparison_dimensions_json=["防御类型", "评估数据集", "正常推荐性能"],
        search_query="defense robust training sequential recommendation poisoning attack",
        term_aliases_json={"鲁棒训练": ["robust training"]},
        task_id=None,
    )
    relevant = _unit(
        text="LoRec provides robust sequential recommendation against poisoning attacks."
    )
    unrelated = _unit(text="Adversarial defense for breast cancer image classification.")
    ranked, _task, _lexical, bridge = _rank_candidates(
        question,
        [unrelated, relevant],
        expected_kinds={"experimental_fact"},
        return_diagnostics=True,
    )
    assert ranked
    assert ranked[0][0] is relevant
    assert bridge == "search_query"


def test_degraded_candidates_are_diverse_by_work() -> None:
    first_work = uuid4()
    crowded = [_unit(text=f"equation {index}") for index in range(8)]
    for unit in crowded:
        unit.work_id = first_work
        unit.grade = "A_located_structured"
    other = [_unit(text=f"result {index}") for index in range(4)]
    degraded = _degraded_candidates([*crowded, *other], task_id=None)
    assert len([unit for unit, _score in degraded if unit.work_id == first_work]) == 2
    assert len({unit.work_id for unit, _score in degraded}) >= 3


def test_ranked_candidates_cannot_be_monopolized_by_one_work() -> None:
    first_work = uuid4()
    first = [_unit(text=f"defense result {index}") for index in range(12)]
    for unit in first:
        unit.work_id = first_work
    other = [_unit(text=f"robustness result {index}") for index in range(3)]
    ranked = [
        *((unit, 1.0 - index / 100) for index, unit in enumerate(first)),
        *((unit, 0.5 - index / 100) for index, unit in enumerate(other)),
    ]
    selected = _diverse_ranked_candidates(ranked, limit=10, per_work_limit=4)
    assert len([unit for unit, _score in selected if unit.work_id == first_work]) == 4
    assert len({unit.work_id for unit, _score in selected}) >= 2


def test_scope_normalizes_grouped_eligibility_criteria() -> None:
    scope = normalize_scope(
        {
            "keyword_groups": [
                {"name": "domain", "keywords": ["sequential recommendation"]},
                {"name": "topic", "keywords": ["poisoning attack"]},
            ],
            "eligibility_criteria": {
                "required_anchor_groups": [
                    {"name": "domain", "terms": ["sequential recommendation"]},
                    {"name": "topic", "terms": ["poisoning attack", "defense"]},
                ],
                "exclusion_domains": ["medical imaging", "云安全"],
            },
        },
        topic="序列推荐攻击",
        language="zh",
    )
    criteria = scope["eligibility_criteria"]
    assert criteria is not None
    assert len(criteria["required_anchor_groups"]) == 2
    assert criteria["exclusion_domains"] == ["medical imaging"]


def test_ir_has_no_empty_runs_after_downgrade() -> None:
    paragraphs = [
        {
            "sentences": [
                {
                    "text": "攻击成功率显著提升。",
                    "cite_keys": [],
                    "evidence_ids": [],
                },
                {
                    "text": "在 MovieLens 上 HR@20 达到 0.18。",
                    "cite_keys": ["wang2025"],
                    "evidence_ids": ["missing"],
                },
            ]
        }
    ]
    cleaned = enforce_sentence_evidence_rules(
        paragraphs,
        evidence_by_id={},
        language="zh",
    )
    draft = SectionDraft(section_key="s1", title="t", paragraphs=cleaned)
    section = draft.to_ir_section()
    empty_text_runs = [
        run for block in section.blocks for run in block.runs if getattr(run, "v", None) == ""
    ]
    assert empty_text_runs == []
    assert all(
        str(sentence.get("text") or "").strip()
        for paragraph in cleaned
        for sentence in paragraph.get("sentences") or []
    )


def test_writing_skips_llm_without_citekeys() -> None:
    section = {
        "key": "s1",
        "title": "攻击方法",
        "kind": "body",
        "cite_keys": [],
        "question_id": str(uuid4()),
    }
    context = WritingContext(
        outline={"sections": [section], "sub_question_bundles": []},
        language="zh",
    )

    class _Boom:
        enabled = True

        async def agenerate_json(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("LLM must not be called for empty cite whitelist")

    draft = asyncio.run(
        write_section(
            section=section,
            cards={},
            whitelist=set(),
            context=context,
            runner=_Boom(),  # type: ignore[arg-type]
        )
    )
    assert draft.generator == "evidence_gap_skeleton"
    assert draft.paragraphs
    assert "尚无满足" in draft.paragraphs[0]["text"] or "证据" in draft.paragraphs[0]["text"]


def test_domain_ontology_infers_recsys_task() -> None:
    tasks = [
        TaskSpec(
            slug="recsys.poisoning_attack",
            domain="recsys_attack",
            inclusion_cues=("sequential recommendation", "poisoning attack"),
            exclusion_cues=("intrusion detection", "blockchain"),
        ),
        TaskSpec(
            slug="bgc.identification",
            domain="bgc",
            inclusion_cues=("biosynthetic gene cluster", "bgc"),
            exclusion_cues=("RNA secondary structure",),
        ),
    ]
    text = "A sequential recommendation poisoning attack on MovieLens."
    assert infer_task_id(text, tasks) == "recsys.poisoning_attack"
    assert topical_status(text, tasks) == "on_topic"
    assert topical_status("Cloud intrusion detection with blockchain", tasks) == "off_topic"


def test_search_queries_reject_mixed_cjk() -> None:
    scope = {
        "keyword_groups": [
            {"name": "method", "keywords": ["deep learning", "基于序列特征的BGC识别模型"]}
        ],
        "sub_questions": [
            {"text": "x", "search_query": "基于序列特征的BGC识别模型"},
            {"text": "y", "search_query": "biosynthetic gene cluster deep learning"},
        ],
        "subtopics": [],
    }
    queries = search_queries(scope, max_queries=12)
    assert all(not any("\u4e00" <= ch <= "\u9fff" for ch in query) for query in queries)
    assert any("biosynthetic" in query for query in queries)


def test_screen_uncertain_is_isolated(monkeypatch) -> None:
    """Uncertain decisions must leave the selected set (N3)."""

    class _Entry:
        def __init__(self) -> None:
            self.user_pinned = False
            self.status = "selected"

    entry = _Entry()
    work = SimpleNamespace(
        id=uuid4(),
        canonical_title="Adversarial Threats to Cloud IDS",
        abstract="intrusion detection for cloud networks",
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    statuses: list[str] = []

    async def _list_entries(session, project_id, status="selected", **kwargs):
        return [(entry, work)]

    async def _upsert(*args, **kwargs):
        return None

    async def _set_status(session, row, status):
        statuses.append(status)
        row.status = status

    monkeypatch.setattr("paperforge_worker.pipelines.screen.list_entries", _list_entries)
    monkeypatch.setattr("paperforge_worker.pipelines.screen.upsert_eligibility_decision", _upsert)
    monkeypatch.setattr("paperforge_worker.pipelines.screen.set_entry_status", _set_status)

    context = SimpleNamespace(
        project_id=uuid4(),
        session=lambda: _Session(),
    )
    outcome = asyncio.run(
        screen_eligibility(
            context,  # type: ignore[arg-type]
            scope={
                "eligibility_criteria": {
                    "required_anchor_facets": ["sequential recommendation"],
                }
            },
        )
    )
    assert outcome.uncertain == 1
    assert statuses == ["candidate_uncertain"]


def test_screen_requires_every_anchor_group(monkeypatch) -> None:
    class _Entry:
        def __init__(self) -> None:
            self.user_pinned = False
            self.literature_role = "general"
            self.status = "selected"

    relevant_entry, irrelevant_entry = _Entry(), _Entry()
    relevant = SimpleNamespace(
        id=uuid4(),
        canonical_title="Robust Sequential Recommendation against Poisoning Attacks",
        abstract="A defense for session-based recommenders.",
    )
    irrelevant = SimpleNamespace(
        id=uuid4(),
        canonical_title="Adversarial Defense for Medical Imaging",
        abstract="A poisoning defense for breast cancer classification.",
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def _list_entries(session, project_id, status="selected", **kwargs):
        return (
            [(relevant_entry, relevant), (irrelevant_entry, irrelevant)]
            if status == "selected"
            else []
        )

    async def _upsert(*args, **kwargs):
        return None

    async def _set_status(session, entry, status):
        entry.status = status

    monkeypatch.setattr("paperforge_worker.pipelines.screen.list_entries", _list_entries)
    monkeypatch.setattr("paperforge_worker.pipelines.screen.upsert_eligibility_decision", _upsert)
    monkeypatch.setattr("paperforge_worker.pipelines.screen.set_entry_status", _set_status)
    outcome = asyncio.run(
        screen_eligibility(
            SimpleNamespace(project_id=uuid4(), session=lambda: _Session()),
            scope={
                "eligibility_criteria": {
                    "required_anchor_groups": [
                        {
                            "name": "domain",
                            "terms": ["sequential recommendation", "session-based recommender"],
                        },
                        {"name": "topic", "terms": ["poisoning", "defense"]},
                    ]
                }
            },
        )
    )
    assert outcome.included == 1
    assert outcome.uncertain == 1
    assert relevant_entry.status == "selected"
    assert irrelevant_entry.status == "candidate_uncertain"


def test_screen_promotes_matching_search_candidate(monkeypatch) -> None:
    class _Entry:
        user_pinned = False
        literature_role = "general"
        status = "candidate"

    entry = _Entry()
    work = SimpleNamespace(
        id=uuid4(),
        canonical_title="Defending Profile Pollution Attacks on Sequential Recommenders",
        abstract="A robust defense against poisoning in next-item recommendation.",
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def _list_entries(session, project_id, status="selected", **kwargs):
        return [(entry, work)] if status == "candidate" else []

    async def _upsert(*args, **kwargs):
        return None

    async def _set_status(session, row, status):
        row.status = status

    monkeypatch.setattr("paperforge_worker.pipelines.screen.list_entries", _list_entries)
    monkeypatch.setattr("paperforge_worker.pipelines.screen.upsert_eligibility_decision", _upsert)
    monkeypatch.setattr("paperforge_worker.pipelines.screen.set_entry_status", _set_status)
    outcome = asyncio.run(
        screen_eligibility(
            SimpleNamespace(project_id=uuid4(), session=lambda: _Session()),
            scope={
                "eligibility_criteria": {
                    "required_anchor_groups": [
                        {
                            "name": "domain",
                            "terms": ["sequential recommender", "next-item recommendation"],
                        },
                        {"name": "security", "terms": ["poisoning", "defense"]},
                    ]
                }
            },
        )
    )
    assert outcome.included == 1
    assert entry.status == "selected"


def test_screen_short_acronym_requires_token_boundary() -> None:
    text = "ketosynthase domain catalyzes polyketide chain biosynthesis"

    assert _phrase_in_text(text, "ai") is False
    assert _phrase_in_text("ai assisted polyketide discovery", "ai") is True
    assert _phrase_in_text("an ai-assisted workflow", "ai") is True


def test_screen_caps_automatic_candidate_promotions(monkeypatch) -> None:
    class _Entry:
        user_pinned = False
        literature_role = "general"

        def __init__(self) -> None:
            self.status = "candidate"

    rows = [
        (
            _Entry(),
            SimpleNamespace(
                id=uuid4(),
                canonical_title=f"AI polyketide discovery study {index}",
                abstract="Machine learning for natural product design.",
            ),
        )
        for index in range(5)
    ]

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def _list_entries(session, project_id, status="selected", **kwargs):
        return rows if status == "candidate" else []

    async def _upsert(*args, **kwargs):
        return None

    async def _set_status(session, entry, status):
        entry.status = status

    monkeypatch.setattr("paperforge_worker.pipelines.screen.list_entries", _list_entries)
    monkeypatch.setattr("paperforge_worker.pipelines.screen.upsert_eligibility_decision", _upsert)
    monkeypatch.setattr("paperforge_worker.pipelines.screen.set_entry_status", _set_status)
    outcome = asyncio.run(
        screen_eligibility(
            SimpleNamespace(
                project_id=uuid4(),
                session=lambda: _Session(),
                settings=SimpleNamespace(search_auto_select_top_k=2),
            ),
            scope={
                "eligibility_criteria": {
                    "required_anchor_groups": [
                        {"name": "domain", "terms": ["polyketide"]},
                        {"name": "method", "terms": ["ai", "machine learning"]},
                    ]
                }
            },
        )
    )

    assert outcome.included == 5
    assert outcome.selected == 2
    assert outcome.budget_limited == 3
    assert [entry.status for entry, _work in rows].count("selected") == 2


def test_repair_screen_round_robins_question_coverage_and_preserves_corpus(
    monkeypatch,
) -> None:
    class _Entry:
        user_pinned = False
        literature_role = "general"

        def __init__(self, status: str, relevance: float) -> None:
            self.status = status
            self.relevance_score = relevance

    rows = [
        (
            _Entry("selected", 0.99),
            SimpleNamespace(
                id=uuid4(),
                canonical_title="Review domain method established corpus",
                abstract="Foundational background.",
            ),
        ),
        *[
            (
                _Entry("candidate", 0.9 - index / 100),
                SimpleNamespace(
                    id=uuid4(),
                    canonical_title=f"Review domain method alpha study {index}",
                    abstract="Alpha endpoint evidence.",
                ),
            )
            for index in range(3)
        ],
        (
            _Entry("candidate", 0.1),
            SimpleNamespace(
                id=uuid4(),
                canonical_title="Review domain method beta study",
                abstract="Beta endpoint evidence.",
            ),
        ),
    ]

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def _list_entries(session, project_id, status="selected", **kwargs):
        return [(entry, work) for entry, work in rows if entry.status == status]

    async def _upsert(*args, **kwargs):
        return None

    async def _set_status(session, entry, status):
        entry.status = status

    monkeypatch.setattr("paperforge_worker.pipelines.screen.list_entries", _list_entries)
    monkeypatch.setattr("paperforge_worker.pipelines.screen.upsert_eligibility_decision", _upsert)
    monkeypatch.setattr("paperforge_worker.pipelines.screen.set_entry_status", _set_status)

    outcome = asyncio.run(
        screen_eligibility(
            SimpleNamespace(
                project_id=uuid4(),
                session=lambda: _Session(),
                settings=SimpleNamespace(search_auto_select_top_k=1),
            ),
            scope={"eligibility_criteria": {"required_anchor_facets": ["review domain"]}},
            focus_questions=[
                {"text": "alpha endpoint"},
                {"text": "beta endpoint"},
            ],
            preserve_selected=True,
            additional_budget=2,
        )
    )

    selected_titles = {work.canonical_title for entry, work in rows if entry.status == "selected"}
    assert rows[0][0].status == "selected"
    assert any("alpha" in title for title in selected_titles)
    assert any("beta" in title for title in selected_titles)
    assert outcome.selected == 3
