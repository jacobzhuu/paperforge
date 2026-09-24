from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from latex_render import TexdClient, build_latex_project
from paper_ir import PaperIR, PaperMeta, render_markdown
from paper_ir.mathematics import source_formulas, valid_math
from paperforge_worker.pipelines.document import _draft_from_section
from paperforge_worker.pipelines.evidence import _evidence_candidates
from paperforge_worker.pipelines.outline import CardBrief, generate_outline
from paperforge_worker.pipelines.scholarly_content import (
    accept_math,
    balanced_evidence,
    formula_catalog,
    math_quality_issues,
    thematic_tables,
)
from paperforge_worker.pipelines.writing import (
    SectionDraft,
    WritingContext,
    coherence_pass,
    write_section,
)
from test_writing_pipeline import _runner


def evidence(key="a", **kw):
    return {
        "evidence_id": f"e-{key}",
        "cite_key": key,
        "work_id": key,
        "title": f"Study {key}",
        "grade": "B_located_prose",
        "section_path": "Methods",
        "page": 2,
        "text": r"The loss is $$L = \sum_i (y_i - x_i)^2$$ where x is prediction and y is target. "
        "The method assumes independent samples.",
        **kw,
    }


def draft_with_math():
    catalog = formula_catalog([evidence()], [])
    key = next(iter(catalog))
    draft = SectionDraft(
        section_key="q1",
        title="Loss",
        paragraphs=[
            {
                "text": f"The loss is {{{{math:{key}}}}}; see {{{{eq:{key}}}}}.",
                "sentences": [
                    {
                        "text": f"The loss is {{{{math:{key}}}}}; see {{{{eq:{key}}}}}.",
                        "cite_keys": ["a"],
                        "evidence_ids": ["e-a"],
                    }
                ],
                "cite_keys": ["a"],
            }
        ],
    )
    accept_math(
        draft,
        {
            "equations": [
                {
                    "source_id": key,
                    "after_paragraph": 0,
                    "explanation": (
                        "Here x is prediction and y is target; independent samples are assumed."
                    ),
                }
            ]
        },
        catalog,
    )
    return draft


@pytest.mark.parametrize(
    "latex",
    [
        r"\input{secret}",
        r"\def\x{1}",
        r"\write18{pwd}",
        r"\csname input\endcsname",
        "x={",
        "x^^41",
        r"\begin{document}x\end{document}",
        "x$y",
    ],
)
def test_unsafe_or_broken_math_rejected(latex):
    assert not valid_math(latex)


def test_source_extraction_keeps_complete_expression_and_context():
    rows = source_formulas(evidence()["text"])
    assert len(rows) == 1
    assert rows[0]["latex"] == r"L = \sum_i (y_i - x_i)^2"
    assert "prediction" in rows[0]["context"]
    assert not source_formulas("Equation 3 defines the loss.")
    assert not source_formulas(r"The loss is $$L=\frac{x}{y}")
    assert not formula_catalog([evidence(grade="D_abstract_only")], [])


def test_math_roundtrip_resume_citations_and_exports():
    draft = draft_with_math()
    section = draft.to_ir_section()
    ir = PaperIR(meta=PaperMeta(title="Source-bound math", language="en"), sections=[section])
    assert ir.collect_cite_keys() == {"a"}
    assert any(b.type == "equation" for b in section.blocks)
    assert any(r.t == "math_inline" for b in section.blocks for r in getattr(b, "runs", []))
    row = SimpleNamespace(
        section_key="q1",
        title="Loss",
        model="test",
        status="generated",
        parent_key=None,
        generation_json=draft.generation,
        body_ir_json=section.model_dump(mode="json"),
    )
    resumed = _draft_from_section(row)
    assert resumed.to_ir_section().model_dump() == section.model_dump()
    assert "Equation (1)" in render_markdown(ir)
    files = build_latex_project(ir).files
    tex = "\n".join(files.values())
    assert r"\begin{equation}" in tex and r"\sum_i" in tex
    assert "{{math:" not in tex and "{{eq:" not in tex
    assert math_quality_issues([row]) == []


async def test_writer_emits_source_formula_and_polish_cannot_drop_inline_math():
    row = evidence()
    catalog = formula_catalog([row], [])
    key = next(iter(catalog))
    section = {"key": "q1", "title": "Loss", "question_id": "q", "cite_keys": ["a"]}
    context = WritingContext(
        outline={
            "sections": [section],
            "sub_question_bundles": [{"question_id": "q", "evidence": [row]}],
        },
        language="en",
    )
    payload = {
        "paragraphs": [
            {
                "sentences": [
                    {
                        "text": f"The method uses {{{{math:{key}}}}}.",
                        "cite_keys": ["a"],
                        "evidence_ids": ["e-a"],
                    }
                ]
            }
        ],
        "equations": [
            {
                "source_id": key,
                "after_paragraph": 0,
                "explanation": "Here x is prediction and y is target.",
            }
        ],
    }
    runner, provider = _runner([payload])
    draft = await write_section(
        section=section, cards={}, whitelist={"a"}, context=context, runner=runner
    )
    assert len(draft.equations) == 1
    assert "SOURCE_FORMULAS" in provider.requests[0].user_prompt
    assert any(b.type == "equation" for b in draft.to_ir_section().blocks)
    before = draft.to_ir_section().model_dump()
    runner, _ = _runner(
        [
            {
                "paragraphs": [
                    {
                        "sentences": [
                            {
                                "sentence_id": "0:0",
                                "text": "The method uses a different objective.",
                                "cite_keys": ["a"],
                                "evidence_ids": ["e-a"],
                                "source_refs": [],
                            }
                        ]
                    }
                ]
            }
        ]
    )
    await coherence_pass(draft=draft, context=context, whitelist={"a"}, runner=runner)
    assert draft.generation["polish_result"]["reason"] == "math_binding"
    assert draft.to_ir_section().model_dump() == before


def test_unknown_formula_is_omitted_with_issue():
    draft = SectionDraft(
        section_key="q1",
        title="Methods",
        paragraphs=[{"sentences": [{"text": "See {{math:invented}}.", "cite_keys": []}]}],
    )
    accept_math(
        draft,
        {
            "equations": [
                {"source_id": "invented", "after_paragraph": 0, "explanation": "No source."}
            ]
        },
        {},
    )
    assert not draft.equations
    assert draft.generation["formula_issues"]
    assert "{{math:" not in str(draft.to_ir_section().model_dump())


def test_asset_formula_is_bound_to_asset():
    catalog = formula_catalog(
        [], [{"_asset_ref": "ua:1", "text": r"Loss $$L=x^2$$; x is residual."}]
    )
    assert next(iter(catalog.values()))["source_refs"] == ["ua:1"]
    assert not next(iter(catalog.values()))["cite_keys"]


def test_budget_never_truncates_formula():
    rows = [evidence(str(i), text="where definition " * 200 + f" $$L_{i}=x^2$$") for i in range(40)]
    catalog = formula_catalog(rows, [])
    assert len(catalog) < 40
    assert all(valid_math(v["latex"]) and v["source_text"].endswith("$$") for v in catalog.values())


def test_evidence_keeps_non_numeric_method_and_formula():
    rows = _evidence_candidates(
        fulltext=r"""[[SECTION=Methods | EQ=loss]]
$$L=x+y$$
[[SECTION=Methods]]
The method assumes independent observations and defines x as prediction.""",
        abstract=None,
        quotable_points=[],
        fulltext_used=True,
    )
    assert any("$$L=x+y$$" in r.text for r in rows)
    assert any("assumes independent" in r.text for r in rows)
    balanced = balanced_evidence(
        [evidence(str(i), text="The result is strong.") for i in range(20)] + [evidence("math")]
    )
    assert balanced[0]["cite_key"] == "math"  # formula evidence gets an early slot


def measurement(key="same", value=0.8):
    return {
        "comparability_key": key,
        "dataset": "D",
        "metric_name": "accuracy",
        "value": value,
        "unit": "",
        "split": "test",
    }


def test_thematic_tables_require_shared_dimensions_and_conditions():
    section = {"key": "q1", "title": "Comparison"}
    assert not thematic_tables(
        section, [evidence(text="A result."), evidence("b", text="A result.")], language="en"
    )
    rows = [
        evidence(text="A result.", measurements=[measurement()]),
        evidence("b", text="A result.", measurements=[measurement(value=0.9)]),
    ]
    tables = thematic_tables(section, rows, language="en")
    assert len(tables) == 1 and len(tables[0]["headers"]) == 4
    assert tables[0]["cite_keys"] == ["a", "b"]
    rows[1]["measurements"][0]["comparability_key"] = "different"
    assert not thematic_tables(section, rows, language="en")
    rows[1]["measurements"][0]["comparability_key"] = "same"
    rows[1]["measurements"][0]["dataset"] = "another"
    assert not thematic_tables(section, rows, language="en")
    assert not thematic_tables(
        section,
        [evidence(str(i), text="Result.", measurements=[measurement()]) for i in range(40)],
        language="en",
    )


async def test_library_matrix_is_only_in_independent_attachment():
    rows = [evidence(), evidence("b")]
    result = await generate_outline(
        topic="Methods",
        research_question="How?",
        language="en",
        cards=[CardBrief(cite_key="a", title="A"), CardBrief(cite_key="b", title="B")],
        whitelist={"a", "b"},
        runner=None,
        sub_question_bundles=[{"question_id": "q", "question": "How?", "evidence": rows}],
    )
    sections = result.tree["sections"]
    assert not next(
        s for s in sections if s.get("synthesis_kind") == "comparison_limitations_conflicts"
    )["inline_tables"]
    ledger = next(s for s in sections if s["key"] == "evidence_ledger")
    assert any(t["label"] == "tab:literature-matrix" for t in ledger["inline_tables"])


def test_real_math_pdf_compiles():
    url = os.getenv("PAPERFORGE_TEST_TEXD_URL")
    if not url:
        pytest.skip("requires running texd")
    ir = PaperIR(
        meta=PaperMeta(title="Mathematical methods", language="en"),
        sections=[draft_with_math().to_ir_section()],
    )
    project = build_latex_project(ir)
    client = TexdClient(url)
    try:
        result = client.compile(project.files, entrypoint=project.entrypoint)
    finally:
        client.close()
    assert result.ok, result.log
    assert result.pdf.startswith(b"%PDF")


def test_latex_ingestion_preserves_inline_fraction_and_alignment():
    from ingest.latex_source import extract_latex_source_content

    parsed = extract_latex_source_content(
        content=rb"""\documentclass{article}
\begin{document}\section{Methods}
We define $z=\frac{x}{y}$ where y is nonzero.
\begin{align}L &= x^2 \\ R &= y^2\end{align}
\end{document}"""
    )
    expressions = source_formulas(parsed.text)
    assert any(r"\frac{x}{y}" in f["latex"] for f in expressions)
    assert any(r"\begin{aligned}" in f["latex"] for f in expressions)
    assert not source_formulas("Equation 1 defines x = some value.", object_ref="eq:1")


def test_table_requires_comparison_purpose():
    rows = [
        evidence("a", measurements=[measurement()]),
        evidence("b", measurements=[measurement()]),
    ]
    assert not thematic_tables({"key": "q", "title": "Background"}, rows, language="en")


def test_missing_math_source_is_reported_only_for_mathematical_section():
    mathematical = SectionDraft(section_key="s1", title="Loss function")
    qualitative = SectionDraft(section_key="s2", title="Social implications")
    accept_math(mathematical, {}, {})
    accept_math(qualitative, {}, {})
    assert mathematical.generation["formula_issues"][0]["code"] == "formula_evidence_missing"
    assert qualitative.generation["formula_issues"] == []


def test_jats_inline_and_display_math_remain_source_expressions():
    from ingest.jats import extract_jats_content

    parsed = extract_jats_content(
        content=b"""<article><body><sec><title>Methods</title>
    <p>Define <inline-formula><tex-math>x_i</tex-math></inline-formula> as the input.</p>
    <disp-formula id="loss"><label>(1)</label><tex-math>L=x_i^2</tex-math></disp-formula>
    </sec></body></article>"""
    )
    expressions = {item["latex"] for item in source_formulas(parsed.text)}
    assert "x_i" in expressions and "L=x_i^2" in expressions


async def test_formula_survives_database_save_and_resume(session_factory):
    from db import create_document, get_writing_whitelist, list_sections
    from paperforge_worker.pipelines.document import _persist_draft
    from test_document_persistence import _context, _seed

    project_id, key = await _seed(session_factory)
    async with session_factory() as session:
        document = await create_document(session, project_id=project_id, outline_id=None)
        document_id = document.id
        whitelist = await get_writing_whitelist(session, project_id)
        await session.commit()
    draft = draft_with_math()
    draft.generator = "llm:test"
    # Only math cites the valid source; the invalid prose citation must be stripped.
    for paragraph in draft.paragraphs:
        paragraph["cite_keys"] = ["offwhitelist"]
        for sentence in paragraph["sentences"]:
            sentence["cite_keys"] = ["offwhitelist"]
    for item in [*draft.math_catalog.values(), *draft.equations]:
        item["cite_keys"] = [key]
    context = _context(project_id, session_factory)
    await _persist_draft(
        context,
        document_id=document_id,
        draft=draft,
        order_no=0,
        whitelist=whitelist,
        language="en",
    )
    async with session_factory() as session:
        saved = (await list_sections(session, document_id))[0]
        assert saved.cite_keys_json == [key]
        original = saved.body_ir_json
        resumed = _draft_from_section(saved)
    await _persist_draft(
        context,
        document_id=document_id,
        draft=resumed,
        order_no=0,
        whitelist=whitelist,
        language="en",
    )
    async with session_factory() as session:
        saved = (await list_sections(session, document_id))[0]
        assert saved.body_ir_json == original
        assert any(b["type"] == "equation" for b in original["blocks"])


async def test_original_paper_uses_user_method_formula():
    asset = {
        "_asset_ref": "ua_method",
        "_asset_kind": "method_note",
        "type": "note",
        "text": "The method minimizes $$L=x^2$$ where x is the residual.",
        "numbers": [],
    }
    key = next(iter(formula_catalog([], [asset])))
    section = {"key": "s2", "title": "Method", "grounding": "assets", "cite_keys": []}
    context = WritingContext(outline={"sections": [section]}, paper_type="original", language="en")
    runner, _ = _runner(
        [
            {
                "paragraphs": [
                    {
                        "sentences": [
                            {
                                "text": "The method minimizes the residual.",
                                "source_refs": ["ua_method"],
                                "cite_keys": [],
                            }
                        ]
                    }
                ],
                "equations": [
                    {"source_id": key, "after_paragraph": 0, "explanation": "x is the residual."}
                ],
            }
        ]
    )
    draft = await write_section(
        section=section, cards={}, whitelist=set(), context=context, runner=runner, assets=[asset]
    )
    ir = PaperIR(meta=PaperMeta(title="User method"), sections=[draft.to_ir_section()])
    assert draft.equations
    assert ir.collect_asset_refs() == {"ua_method"}
