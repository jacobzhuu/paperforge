"""M3 测试：LaTeX 工程装配、R3 契约、确定性编译修复。

反例优先：没有持久化 bibtex_key 就不许生成书目；未定义环境/缺失宏包必须被降级
而不是让整篇编不出来；LLM 修补器不得触碰导言区与 refs.bib。
"""

from __future__ import annotations

import io
import os

import httpx
import pytest
from latex_render import (
    TEMPLATES,
    CompileOutcome,
    TexdClient,
    bibliography_broken,
    build_latex_project,
    compile_with_repair,
    deterministic_repairs,
    error_context,
    render_inline_bibliography,
    resolve_template,
    template_fallback_warning,
    with_inline_bibliography,
)
from paper_ir import (
    CiteRun,
    PaperAuthor,
    PaperIR,
    PaperMeta,
    ParagraphBlock,
    ReferenceMetadata,
    Section,
    TextRun,
)
from paper_ir.schema import EquationBlock, TableBlock, TableSource, TodoBlock


def _ref(**overrides) -> ReferenceMetadata:
    payload = {
        "work_key": "w1",
        "bibtex_key": "lewis2020retrieval",
        "title": "Retrieval-Augmented Generation",
        "publication_year": 2020,
        "venue_name": "NeurIPS",
        "citation_metadata": {"authors": [{"author_name": "Patrick Lewis", "author_order": 1}]},
    }
    payload.update(overrides)
    return ReferenceMetadata(**payload)


def _ir(*, language: str = "en") -> PaperIR:
    return PaperIR(
        meta=PaperMeta(
            title="RAG & Science: 100% Coverage",
            language=language,  # type: ignore[arg-type]
            abstract="We survey RAG.",
            keywords=["RAG"],
        ),
        sections=[
            Section(
                key="s1",
                title="Introduction",
                blocks=[
                    ParagraphBlock(
                        runs=[
                            TextRun(v="Costs fell by 50% & accuracy rose #1."),
                            CiteRun(keys=["lewis2020retrieval"]),
                        ]
                    ),
                    EquationBlock(latex="E = mc^2", label="eq:e"),
                    TodoBlock(),
                ],
            )
        ],
    )


def test_project_splits_sections_and_inputs_them() -> None:
    project = build_latex_project(_ir(), references=[_ref()])
    section_files = [path for path in project.files if path.startswith("sections/")]
    assert len(section_files) == 1
    # 章节独立成文件：编译报错行号能对应到具体章节。
    assert "\\input{sections/00-s1}" in project.files["main.tex"]
    assert "\\section{Introduction}" in project.files[section_files[0]]


def test_project_carries_binary_figures_without_text_coercion() -> None:
    png = b"\x89PNG\r\n\x1a\ncontent"
    project = build_latex_project(
        _ir(), references=[_ref()], figure_files={"figures/result.png": png}
    )
    assert project.binary_files == {"figures/result.png": png}
    payload = project.to_payload()
    assert payload["text_files"]["main.tex"] == project.files["main.tex"]
    assert "figures/result.png" in payload["binary_files"]


def test_special_characters_are_escaped_in_body_and_title() -> None:
    project = build_latex_project(_ir(), references=[_ref()])
    body = project.files["sections/00-s1.tex"]
    assert "50\\%" in body
    assert "\\#1" in body
    assert "100\\%" in project.files["main.tex"]


def test_multiple_authors_use_latex_separator_without_printing_it() -> None:
    ir = _ir()
    ir.meta.authors = ["Ada & Smith", "Lin Chen"]
    project = build_latex_project(ir)
    assert r"Ada \& Smith \and Lin Chen" in project.files["main.tex"]
    assert r"\textbackslash{}and" not in project.files["main.tex"]


def test_structured_authors_render_affiliations_corresponding_email_and_orcid() -> None:
    ir = _ir()
    ir.meta.author_details = [
        PaperAuthor(
            id="a1",
            name="Ada Lovelace",
            affiliations=["Analytical Engine Lab"],
            email="ada@example.org",
            orcid="0000-0002-1825-0097",
            corresponding=True,
        ),
        PaperAuthor(
            id="a2",
            name="Lin Chen",
            affiliations=["Analytical Engine Lab", "Paper Forge Institute"],
        ),
    ]
    project = build_latex_project(ir)
    main = project.files["main.tex"]
    assert r"Ada Lovelace\textsuperscript{1,*}" in main
    assert r"Lin Chen\textsuperscript{1,2}" in main
    assert "Corresponding author: ada@example.org" in main
    assert "ORCID: Ada Lovelace: 0000-0002-1825-0097" in main
    assert r"\small\shortstack{" in main


def test_structured_author_details_compile_in_real_texd() -> None:
    """结构化作者不能只验证字符串；它会在 ``\\maketitle`` 时二次展开。

    本地/部署验收显式传入 ``PAPERFORGE_TEST_TEXD_URL`` 时跑真实
    Tectonic；普通单测环境没有 texd 时保持可移植。
    """
    texd_url = os.getenv("PAPERFORGE_TEST_TEXD_URL")
    if not texd_url:
        pytest.skip("set PAPERFORGE_TEST_TEXD_URL to run the real texd regression")

    ir = _ir(language="zh")
    ir.sections = []
    ir.meta.author_details = [
        PaperAuthor(
            id="a1",
            name="朱子阳",
            affiliations=["北京邮电大学"],
            orcid="0009-0001-1933-1921",
        )
    ]
    project = build_latex_project(ir, template="cn_thesis")
    client = TexdClient(texd_url)
    try:
        outcome = client.compile(project.files, entrypoint=project.entrypoint)
    finally:
        client.close()
    assert outcome.ok, outcome.log
    assert outcome.pdf and outcome.pdf.startswith(b"%PDF")


def test_long_single_column_table_compiles_across_pages_in_real_texd() -> None:
    texd_url = os.getenv("PAPERFORGE_TEST_TEXD_URL")
    if not texd_url:
        pytest.skip("set PAPERFORGE_TEST_TEXD_URL to run the real texd regression")

    rows = [
        [
            f"Study {index}: " + "complete research title " * 5,
            str(2000 + index),
            "Detailed method and setting " * 4,
            "Located full text",
        ]
        for index in range(1, 21)
    ]
    rows[-1][0] = "LASTROW"
    ir = PaperIR(
        meta=PaperMeta(title="Long table regression", language="en"),
        sections=[
            Section(
                key="synthesis",
                title="Synthesis",
                blocks=[
                    TableBlock(
                        caption="Methods and evidence",
                        label="tab:evidence",
                        source=TableSource(
                            kind="inline",
                            data={
                                "headers": ["Study", "Year", "Method", "Evidence"],
                                "rows": rows,
                            },
                        ),
                    )
                ],
            )
        ],
    )
    project = build_latex_project(ir, template="article")
    client = TexdClient(texd_url)
    try:
        outcome = client.compile(project.files, entrypoint=project.entrypoint)
    finally:
        client.close()
    assert outcome.ok, outcome.log
    assert outcome.pdf

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(outcome.pdf))
    text = " ".join(
        " ".join((page.extract_text() or "").split()) for page in reader.pages
    )
    assert len(reader.pages) >= 2
    assert "LASTROW" in text


def test_ieee_structured_authors_use_ieee_blocks() -> None:
    ir = _ir()
    ir.meta.author_details = [
        PaperAuthor(id="a1", name="Ada Lovelace", affiliations=["Engine Lab"]),
    ]
    main = build_latex_project(ir, template="ieee").files["main.tex"]
    assert r"\IEEEauthorblockN{Ada Lovelace\textsuperscript{1}}" in main
    assert r"\IEEEauthorblockA{\textsuperscript{1} Engine Lab}" in main


def test_todo_block_renders_visible_placeholder() -> None:
    """不编造实验数据：占位符必须在 PDF 里显眼（红色 TODO）。"""
    project = build_latex_project(_ir(), references=[_ref()])
    assert "\\todo{" in project.files["sections/00-s1.tex"]
    assert "\\providecommand{\\todo}" in project.files["main.tex"]


def test_bibliography_is_generated_from_library_metadata() -> None:
    project = build_latex_project(_ir(), references=[_ref()])
    assert "@article{lewis2020retrieval" in project.files["refs.bib"]
    assert "\\bibliography{refs}" in project.files["main.tex"]


def test_reference_without_persisted_key_is_refused_not_invented() -> None:
    """R3：缺持久化 key 属于契约破坏——交付无参考文献版本 + 告警，绝不现编 key。"""
    project = build_latex_project(_ir(), references=[_ref(bibtex_key=None)])
    assert "refs.bib" not in project.files
    assert "\\bibliography{refs}" not in project.files["main.tex"]
    assert project.warnings and project.warnings[0]["stage"] == "bibtex"


def test_chinese_project_loads_ctex() -> None:
    project = build_latex_project(_ir(language="zh"), template="gbt7714")
    assert "\\usepackage{ctex}" in project.files["main.tex"]
    assert "关键词" in project.files["main.tex"]


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_template_renders(name: str) -> None:
    project = build_latex_project(_ir(), references=[_ref()], template=name)
    assert "\\begin{document}" in project.files["main.tex"]
    assert "\\end{document}" in project.files["main.tex"]


def test_unknown_template_falls_back_to_article() -> None:
    assert resolve_template("nonexistent-venue") == "article"
    assert resolve_template(None) == "article"


# ---- 确定性修复 ----


def test_missing_package_is_dropped_not_fatal() -> None:
    files = {
        "main.tex": (
            "\\documentclass{article}\n\\usepackage{gbt7714}\n\\begin{document}x\\end{document}"
        )
    }
    log = "LaTeX Error: File `gbt7714.sty' not found."
    repaired, actions = deterministic_repairs(files, log)
    assert "% [paperforge] dropped missing package" in repaired["main.tex"]
    assert actions[0]["kind"] == "drop_missing_package"


def test_undefined_environment_is_downgraded_to_plain_text() -> None:
    files = {"sections/00-s1.tex": "\\begin{theorem}important claim\\end{theorem}"}
    log = "LaTeX Error: Environment theorem undefined"
    repaired, actions = deterministic_repairs(files, log)
    # 内容保住了，环境去掉了。
    assert "important claim" in repaired["sections/00-s1.tex"]
    assert "\\begin{theorem}" not in repaired["sections/00-s1.tex"]
    assert actions[0]["kind"] == "drop_undefined_environment"


def test_whitelisted_environment_is_never_dropped() -> None:
    files = {"sections/00-s1.tex": "\\begin{equation}E=mc^2\\end{equation}"}
    log = "LaTeX Error: Environment equation undefined"
    _repaired, actions = deterministic_repairs(files, log)
    assert actions == []


def test_undefined_command_is_removed() -> None:
    files = {"sections/00-s1.tex": "text \\madeupcommand more text"}
    log = "! Undefined control sequence.\nl.5 \\madeupcommand"
    repaired, actions = deterministic_repairs(files, log)
    assert "\\madeupcommand" not in repaired["sections/00-s1.tex"]
    assert actions[0]["kind"] == "drop_undefined_command"


def test_error_context_extracts_line_numbers() -> None:
    log = "! Undefined control sequence.\nl.42 \\foo bar\nl.99 \\baz"
    assert error_context(log)[0] == {"line": 42, "snippet": "\\foo bar"}


def test_error_context_extracts_tectonic_file_and_line() -> None:
    log = "error: sections/00-s1.tex:38: Missing } inserted\nerror: halted"
    assert error_context(log)[0] == {
        "file": "sections/00-s1.tex",
        "line": 38,
        "snippet": "Missing } inserted",
    }


# ---- 编译流程 ----


class _StubTexd(TexdClient):
    def __init__(self, outcomes: list[CompileOutcome]) -> None:
        super().__init__("http://stub")
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, str]] = []

    def compile(self, files, *, entrypoint="main.tex"):  # type: ignore[override]
        self.calls.append(dict(files))
        return self.outcomes.pop(0) if self.outcomes else CompileOutcome(ok=False, log="")


def test_compile_succeeds_without_repair() -> None:
    client = _StubTexd([CompileOutcome(ok=True, pdf=b"%PDF-1.7", log="ok")])
    result = compile_with_repair({"main.tex": "x"}, client=client)
    assert result.ok
    assert result.rounds == 0
    assert len(client.calls) == 1


def test_compile_repairs_then_succeeds() -> None:
    client = _StubTexd(
        [
            CompileOutcome(ok=False, log="LaTeX Error: Environment theorem undefined"),
            CompileOutcome(ok=True, pdf=b"%PDF-1.7", log="ok"),
        ]
    )
    result = compile_with_repair(
        {"main.tex": "\\begin{theorem}x\\end{theorem}"},
        client=client,
    )
    assert result.ok
    assert result.rounds == 1
    assert result.repairs[0]["kind"] == "drop_undefined_environment"


def test_repair_rounds_are_bounded() -> None:
    failing = CompileOutcome(ok=False, log="LaTeX Error: Environment theorem undefined")
    client = _StubTexd([failing, failing, failing, failing])
    result = compile_with_repair(
        {"main.tex": "\\begin{theorem}x\\end{theorem}"},
        client=client,
        max_rounds=2,
    )
    assert not result.ok
    # 首编译 + 至多 2 轮修复 = 3 次；绝不无界重试。
    assert len(client.calls) <= 3


def test_llm_patcher_runs_only_when_deterministic_repair_has_nothing() -> None:
    calls: list[str] = []

    def patcher(files: dict[str, str], log: str) -> dict[str, str]:
        calls.append(log)
        return {**files, "sections/00-s1.tex": "patched"}

    client = _StubTexd(
        [
            CompileOutcome(ok=False, log="! Some error with no known deterministic fix"),
            CompileOutcome(ok=True, pdf=b"%PDF", log="ok"),
        ]
    )
    result = compile_with_repair(
        {"main.tex": "x", "sections/00-s1.tex": "y"},
        client=client,
        patcher=patcher,
    )
    assert result.ok
    assert len(calls) == 1
    assert result.repairs[-1]["kind"] == "llm_patch"


def test_texd_unreachable_degrades_instead_of_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = TexdClient(
        "http://texd.invalid",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = compile_with_repair({"main.tex": "x"}, client=client)
    # Draft-first：编译服务挂了也返回结构化结果，交付工程 + 日志。
    assert not result.ok
    assert "unreachable" in result.log


def test_texd_client_sends_binary_files_as_separate_base64_channel() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.content)
        assert payload["text_files"] == {"main.tex": "x"}
        assert payload["binary_files"]["figures/a.png"] == "iVBORw0KGgpyZXN0"
        return httpx.Response(
            200,
            json={"ok": True, "log": "ok", "pdf_base64": "JVBERi0="},
        )

    client = TexdClient(
        "http://texd.invalid",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.compile(
        {"main.tex": "x"},
        binary_files={"figures/a.png": b"\x89PNG\r\n\x1a\nrest"},
    )
    assert result.ok


# ---- 书目兜底：编译「成功」但引用全是 [?] 的那类静默失败 ----

# 沙箱缓存只读时 Tectonic 的真实日志形状（compile_log-5bbfdbb52102.log）。
_BST_FAILURE_LOG = """note: Running TeX ...
note: Running BibTeX on main.aux ...
note: downloading unsrt.bst
note: Rerunning TeX because bibtex was run ...
warning: open of input unsrt.bst failed
caused by: couldn't open /root/.cache/Tectonic/bundles/data/x/unsrt.bst-tmp-pid12 for writing
caused by: Read-only file system (os error 30)
warning: errors were issued by BibTeX, but were ignored.
"""


def test_bibliography_broken_detects_bst_failure_and_undefined_citations() -> None:
    assert bibliography_broken(_BST_FAILURE_LOG)
    assert bibliography_broken("LaTeX Warning: Citation `lewis2020retrieval' on page 3 undefined")
    assert bibliography_broken("I couldn't open style file unsrt.bst")
    assert not bibliography_broken("note: Running TeX ...\nnote: Writing `main.pdf`")


def test_benign_bibtex_error_does_not_count_as_a_broken_bibliography() -> None:
    """BibTeX 报错 ≠ 书目没排出来。

    实测：gbt7714 模板重复发 `\\bibstyle` 会让 BibTeX 报一个 error，但 .bbl 完好、
    PDF 里 `[1] [2]` 一切正常。凭这行降级，会把投稿方 .bst 的排版换成内联兜底，
    还挂上一个假的「降级」告警。
    """
    log = (
        "note: Running BibTeX on main.aux ...\n"
        "warning: errors were issued by BibTeX, but were ignored; use --print for details.\n"
    )
    assert not bibliography_broken(log)


def test_inline_bibliography_replaces_bibliography_command_only() -> None:
    # gbt7714 模板把 \bibliographystyle 包在 \IfFileExists 的分支里：动它会吃掉右花括号。
    main = (
        "\\IfFileExists{gbt7714.sty}{\\bibliographystyle{gbt7714-numerical}}"
        "{\\bibliographystyle{unsrt}}\n\\bibliography{refs}\n\\end{document}"
    )
    block = "\\begin{thebibliography}{9}\n x \n\\end{thebibliography}"
    patched = with_inline_bibliography({"main.tex": main}, block)
    assert patched is not None
    assert "\\bibliography{refs}" not in patched["main.tex"]
    assert "{\\bibliographystyle{gbt7714-numerical}}" in patched["main.tex"]
    assert "thebibliography" in patched["main.tex"]


def test_inline_bibliography_returns_none_when_nothing_to_replace() -> None:
    assert with_inline_bibliography({"main.tex": "no bibliography here"}, "block") is None
    assert with_inline_bibliography({"main.tex": "\\bibliography{refs}"}, "") is None


def test_compile_falls_back_to_inline_bibliography_when_bibtex_fails() -> None:
    """编译 ok 但书目坏了，也必须重编——否则交付的是一篇全 [?] 的 PDF。"""
    client = _StubTexd(
        [
            CompileOutcome(ok=True, pdf=b"%PDF-broken-refs", log=_BST_FAILURE_LOG),
            CompileOutcome(ok=True, pdf=b"%PDF-good", log="note: Writing `main.pdf`"),
        ]
    )
    result = compile_with_repair(
        {"main.tex": "x\\bibliographystyle{unsrt}\n\\bibliography{refs}"},
        client=client,
        inline_bibliography="\\begin{thebibliography}{9}\n\\bibitem{a} A\n\\end{thebibliography}",
    )
    assert result.ok
    assert result.bibliography_ok
    assert result.pdf == b"%PDF-good"
    assert result.repairs[-1]["kind"] == "inline_bibliography"
    assert "thebibliography" in client.calls[-1]["main.tex"]


def test_inline_bibliography_fallback_is_rejected_if_it_breaks_the_build() -> None:
    """兜底把原本能过的编译搞挂时，保留原成品——降级不该变成回退。"""
    client = _StubTexd(
        [
            CompileOutcome(ok=True, pdf=b"%PDF-broken-refs", log=_BST_FAILURE_LOG),
            CompileOutcome(ok=False, log="! Emergency stop"),
        ]
    )
    result = compile_with_repair(
        {"main.tex": "x\\bibliography{refs}"},
        client=client,
        inline_bibliography="\\begin{thebibliography}{9}\n\\bibitem{a} A\n\\end{thebibliography}",
    )
    assert result.ok
    assert result.pdf == b"%PDF-broken-refs"
    assert not result.bibliography_ok  # 静默失败必须留痕，交给上层报警
    assert not result.repairs


def test_compile_without_fallback_reports_broken_bibliography() -> None:
    client = _StubTexd([CompileOutcome(ok=True, pdf=b"%PDF", log=_BST_FAILURE_LOG)])
    result = compile_with_repair({"main.tex": "x\\bibliography{refs}"}, client=client)
    assert result.ok
    assert not result.bibliography_ok
    assert len(client.calls) == 1


def test_render_inline_bibliography_is_deterministic_and_matches_cite_keys() -> None:
    block = render_inline_bibliography([_ref(), _ref(work_key="w2", bibtex_key="devlin2019bert")])
    assert block.startswith("\\begin{thebibliography}{9}")
    assert "\\bibitem{lewis2020retrieval}" in block
    assert "\\bibitem{devlin2019bert}" in block
    assert block == render_inline_bibliography(
        [_ref(), _ref(work_key="w2", bibtex_key="devlin2019bert")]
    )


def test_render_inline_bibliography_escapes_and_skips_keyless_refs() -> None:
    assert render_inline_bibliography([]) == ""
    assert render_inline_bibliography([_ref(bibtex_key=None)]) == ""
    block = render_inline_bibliography([_ref(title="Cost is 100% & rising")])
    assert "100\\% \\&" in block


def test_cn_thesis_maps_to_the_chinese_template() -> None:
    """界面「中文学位论文/学报」发的是 cn_thesis；它此前静默退回 article。"""
    assert resolve_template("cn_thesis") == "cn_thesis"
    assert TEMPLATES["cn_thesis"] == TEMPLATES["gbt7714"]
    assert template_fallback_warning("cn_thesis") is None
    assert template_fallback_warning("IEEEtran") is None


def test_unimplemented_template_is_reported_instead_of_silently_downgraded() -> None:
    warning = template_fallback_warning("acmart")
    assert warning == {
        "stage": "template",
        "reason": "template_not_implemented",
        "requested": "acmart",
        "used": "article",
    }
    assert resolve_template("acmart") == "article"
    assert template_fallback_warning(None) is None
