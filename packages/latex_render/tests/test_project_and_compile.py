"""M3 测试：LaTeX 工程装配、R3 契约、确定性编译修复。

反例优先：没有持久化 bibtex_key 就不许生成书目；未定义环境/缺失宏包必须被降级
而不是让整篇编不出来；LLM 修补器不得触碰导言区与 refs.bib。
"""

from __future__ import annotations

import httpx
import pytest
from latex_render import (
    TEMPLATES,
    CompileOutcome,
    TexdClient,
    build_latex_project,
    compile_with_repair,
    deterministic_repairs,
    error_context,
    resolve_template,
)
from paper_ir import (
    CiteRun,
    PaperIR,
    PaperMeta,
    ParagraphBlock,
    ReferenceMetadata,
    Section,
    TextRun,
)
from paper_ir.schema import EquationBlock, TodoBlock


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


def test_special_characters_are_escaped_in_body_and_title() -> None:
    project = build_latex_project(_ir(), references=[_ref()])
    body = project.files["sections/00-s1.tex"]
    assert "50\\%" in body
    assert "\\#1" in body
    assert "100\\%" in project.files["main.tex"]


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
            "\\documentclass{article}\n\\usepackage{gbt7714}\n"
            "\\begin{document}x\\end{document}"
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
