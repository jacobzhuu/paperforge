import pytest
from paper_ir import (
    ReferenceMetadata,
    format_scholarly_reference,
    make_bibtex_key,
    parse_bibtex_entries,
    render_bibtex,
    render_markdown,
)


def _ref(**kw):
    meta = {"authors": kw.pop("authors", [])}
    if "url" in kw:
        meta["landing_page_url"] = kw.pop("url")
    return ReferenceMetadata(work_key=kw.pop("work_key", "w1"), citation_metadata=meta, **kw)


def test_bibtex_key_is_firstauthor_year_keyword():
    ref = _ref(
        title="A Survey of Graph Neural Networks",
        publication_year=2023,
        authors=[{"author_name": "Jie Wang", "author_order": 0}],
    )
    assert make_bibtex_key(ref) == "wang2023survey"


def test_bibtex_key_conflict_gets_suffix():
    ref = _ref(
        title="Deep Learning",
        publication_year=2020,
        authors=[{"author_name": "Li Ming", "author_order": 0}],
    )
    taken: set[str] = set()
    k1 = make_bibtex_key(ref, taken=taken)
    k2 = make_bibtex_key(ref, taken=taken)
    assert k1 != k2
    assert k2.startswith(k1)


def test_render_bibtex_contains_entry_and_doi():
    ref = _ref(
        bibtex_key="vaswani2017attention",
        title="Attention Is All You Need",
        publication_year=2017,
        venue_name="NeurIPS",
        doi="10.5555/3295222",
        work_type="conference",
        authors=[{"author_name": "Ashish Vaswani", "author_order": 0}],
    )
    bib = render_bibtex([ref])
    assert "vaswani2017attention" in bib
    assert "10.5555/3295222" in bib


def test_render_bibtex_requires_and_preserves_persisted_key():
    missing = _ref(title="A title")
    try:
        render_bibtex([missing])
    except ValueError as exc:
        assert "no persisted bibtex_key" in str(exc)
    else:
        raise AssertionError("render_bibtex must not recompute a missing key")

    persisted = _ref(bibtex_key="stable2026key", title="A title")
    assert "stable2026key" in render_bibtex([persisted])


def test_bibtex_key_uses_numeric_suffix_after_26_collisions():
    ref = _ref(
        title="Deep Learning",
        publication_year=2020,
        authors=[{"author_name": "Li Ming", "author_order": 0}],
    )
    taken = {make_bibtex_key(ref)}
    taken.update(f"ming2020deep{chr(code)}" for code in range(ord("a"), ord("z") + 1))
    assert make_bibtex_key(ref, taken=taken) == "ming2020deep1"


def test_bibtex_key_for_cjk_metadata_is_ascii_and_stable():
    ref = _ref(
        work_key="work-中文-1",
        title="图神经网络综述",
        publication_year=2024,
        authors=[{"author_name": "王杰", "author_order": 0}],
    )
    first = make_bibtex_key(ref)
    assert first == make_bibtex_key(ref)
    assert first.isascii()


def test_render_bibtex_escapes_latex_special_characters():
    ref = _ref(
        bibtex_key="safe2024key",
        title="A & B: 100%_good",
        publication_year=2024,
        authors=[{"author_name": "Doe, Jane", "author_order": 0}],
    )
    bib = render_bibtex([ref])
    assert r"A \& B: 100\%\_good" in bib


def test_apa_reference_formatting():
    ref = _ref(
        title="Deep Residual Learning",
        publication_year=2016,
        venue_name="CVPR",
        doi="10.1109/cvpr.2016.90",
        authors=[{"author_name": "Kaiming He", "author_order": 0}],
    )
    out = format_scholarly_reference(ref, style="apa")
    assert "(2016)" in out
    assert "Deep Residual Learning" in out


@pytest.mark.parametrize(
    ("author_name", "expected_prefix"),
    [
        ("Lewis, Patrick", "lewis"),
        ("Ada Lovelace", "lovelace"),
        # Europe PMC 的 authorString 是「姓 + 缩写名」，不能一律取最后一段。
        ("Zhang Q", "zhang"),
        ("Gao Y.", "gao"),
        ("J. R. R. Tolkien", "tolkien"),
    ],
)
def test_bibtex_key_uses_surname_not_initials(author_name, expected_prefix):
    ref = ReferenceMetadata(
        work_key="w1",
        title="Retrieval Augmented Generation",
        publication_year=2024,
        citation_metadata={"authors": [{"author_name": author_name, "author_order": 1}]},
    )
    assert make_bibtex_key(ref).startswith(f"{expected_prefix}2024")


def test_bibtex_import_parses_entries_as_unverified_clues():
    entries = parse_bibtex_entries(
        """
        @inproceedings{lewis2020rag,
          title = {Retrieval-Augmented Generation for Knowledge-Intensive {NLP} Tasks},
          author = {Lewis, Patrick and Perez, Ethan},
          year = {2020},
          booktitle = {NeurIPS},
          doi = {10.5555/rag}
        }
        """
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == "lewis2020rag"
    assert entry.doi == "10.5555/rag"
    assert entry.publication_year == 2020
    assert entry.authors == ("Patrick Lewis", "Ethan Perez")
    # 花括号保护被剥离，标题可直接用于标题反查。
    assert entry.title == "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"


def test_bibtex_import_rejects_invalid_input():
    with pytest.raises(ValueError, match="invalid bibtex input"):
        parse_bibtex_entries("@article{broken, title = {unclosed")


def test_markdown_renders_citations_as_numbers_with_reference_list():
    from paper_ir import CiteRun, PaperIR, PaperMeta, ParagraphBlock, Section, TextRun

    ref = _ref(
        work_key="w1",
        bibtex_key="lewis2020retrieval",
        title="Retrieval-Augmented Generation",
        publication_year=2020,
        venue_name="NeurIPS",
        authors=[{"author_name": "Patrick Lewis", "author_order": 1}],
    )
    ir = PaperIR(
        meta=PaperMeta(title="RAG 综述", language="zh", abstract="本文综述 RAG。"),
        sections=[
            Section(
                key="s1",
                title="引言",
                blocks=[
                    ParagraphBlock(
                        runs=[
                            TextRun(v="检索增强生成把生成锚定在证据上。"),
                            CiteRun(keys=["lewis2020retrieval"]),
                        ]
                    )
                ],
            )
        ],
    )
    markdown = render_markdown(ir, references=[ref], style="gbt7714")
    assert "# RAG 综述" in markdown
    assert "**摘要**" in markdown
    assert "## 引言" in markdown
    assert "[1]" in markdown
    assert "## 参考文献" in markdown
    assert "1. " in markdown
    assert "Retrieval-Augmented Generation" in markdown


def test_markdown_surfaces_r2_removal_warnings():
    from paper_ir import CitationWarning, PaperIR, PaperMeta, ParagraphBlock, Section, TextRun

    section = Section(
        key="s1",
        title="Body",
        blocks=[ParagraphBlock(runs=[TextRun(v="text")])],
        citation_warnings=[CitationWarning(path="sections[0]", rejected_keys=("fake2029key",))],
    )
    markdown = render_markdown(PaperIR(meta=PaperMeta(title="t"), sections=[section]))
    # 静默剔除是不可接受的：预览里必须能看到引用被移除。
    assert "⚠️" in markdown
    assert "fake2029key" in markdown


def test_markdown_todo_block_is_prominent():
    from paper_ir import PaperIR, PaperMeta, Section
    from paper_ir.schema import TodoBlock

    ir = PaperIR(
        meta=PaperMeta(title="t"),
        sections=[Section(key="s1", title="Results", blocks=[TodoBlock()])],
    )
    markdown = render_markdown(ir)
    assert "**TODO**" in markdown
    assert "待补充实验数据" in markdown


def test_markdown_renders_marks_and_lists():
    """预览与 LaTeX 用同一套语义：强调来自 marks，不是正文里的星号。"""
    from paper_ir.markdown import render_markdown
    from paper_ir.schema import (
        CiteRun,
        ListBlock,
        ListItem,
        PaperIR,
        PaperMeta,
        ParagraphBlock,
        Section,
        TextRun,
    )

    ir = PaperIR(
        meta=PaperMeta(title="T", language="zh"),
        sections=[
            Section(
                key="s1",
                title="节",
                blocks=[
                    ParagraphBlock(
                        runs=[
                            TextRun(v="粗", marks=["bold"]),
                            TextRun(v="斜", marks=["italic"]),
                        ]
                    ),
                    ListBlock(
                        ordered=True,
                        items=[
                            ListItem(runs=[TextRun(v="一"), CiteRun(keys=["k1"])]),
                            ListItem(runs=[TextRun(v="二")]),
                        ],
                    ),
                    ListBlock(ordered=False, items=[ListItem(runs=[TextRun(v="要点")])]),
                ],
            )
        ],
    )
    md = render_markdown(ir, references=[])
    assert "**粗**" in md
    assert "*斜*" in md
    assert "1. 一" in md
    assert "2. 二" in md
    assert "- 要点" in md


def test_user_asset_grounding_is_preserved_but_invisible_in_outputs():
    """Provenance is audit metadata, never manuscript prose or LaTeX syntax."""
    from latex_render import render_section
    from paper_ir import GroundingRun, PaperIR, PaperMeta, ParagraphBlock, Section, TextRun

    section = Section(
        key="s4",
        title="Results",
        blocks=[
            ParagraphBlock(
                runs=[
                    TextRun(v="The recorded accuracy was 92.5%."),
                    GroundingRun(source_refs=["ua_12345678"]),
                ]
            )
        ],
    )
    ir = PaperIR(meta=PaperMeta(title="Grounded result"), sections=[section])

    assert ir.collect_asset_refs() == {"ua_12345678"}
    assert ir.model_dump()["sections"][0]["blocks"][0]["runs"][1] == {
        "t": "grounding",
        "source_refs": ["ua_12345678"],
    }
    markdown = render_markdown(ir)
    latex = render_section(section)
    assert "The recorded accuracy was 92.5%." in markdown
    assert r"The recorded accuracy was 92.5\%." in latex
    assert "ua_12345678" not in markdown
    assert "ua_12345678" not in latex
