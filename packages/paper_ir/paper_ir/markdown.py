"""PaperIR → Markdown 预览（设计 §4.6「次要导出」）。

与 LaTeX 渲染同源：都只消费 IR，不消费 LLM 原始输出。引用按持久化的
bibtex_key 渲染为 author-year 或编号，参考文献表由库内元数据确定性生成（R3）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from paper_ir.citation_style import CitationStyle, format_scholarly_reference
from paper_ir.reference import ReferenceMetadata
from paper_ir.schema import (
    AlgorithmBlock,
    CiteRun,
    EquationBlock,
    FigureBlock,
    ListBlock,
    MathInlineRun,
    PaperIR,
    ParagraphBlock,
    Section,
    TableBlock,
    TextRun,
    TodoBlock,
    XRefRun,
)

_HEADING = {1: "##", 2: "###", 3: "####"}


def render_markdown(
    ir: PaperIR,
    *,
    references: Iterable[ReferenceMetadata] = (),
    style: CitationStyle | str | None = None,
    asset_urls: Mapping[str, str] | None = None,
) -> str:
    """渲染整篇 Markdown：标题 + 摘要 + 章节 + 参考文献。"""
    refs = list(references)
    numbering = {ref.bibtex_key: index + 1 for index, ref in enumerate(refs) if ref.bibtex_key}
    by_key = {ref.bibtex_key: ref for ref in refs if ref.bibtex_key}
    citation_style = style or ir.bibliography.style
    figure_numbering = {
        block.label: index
        for index, block in enumerate(
            (
                block
                for section in ir.sections
                for block in section.blocks
                if isinstance(block, FigureBlock)
            ),
            start=1,
        )
        if block.label
    }

    parts: list[str] = [f"# {ir.meta.title}".rstrip()]
    if ir.meta.authors:
        parts.append("*" + ", ".join(ir.meta.authors) + "*")
    affiliations: list[str] = []
    for author in ir.meta.author_details:
        for affiliation in author.affiliations:
            if affiliation not in affiliations:
                affiliations.append(affiliation)
    if affiliations:
        parts.append("<small>" + " · ".join(affiliations) + "</small>")
    corresponding = [
        f"{author.name} ({author.email})"
        for author in ir.meta.author_details
        if author.corresponding and author.email
    ]
    if corresponding:
        parts.append("<small>Corresponding author: " + ", ".join(corresponding) + "</small>")
    if ir.meta.abstract:
        heading = "摘要" if ir.meta.language == "zh" else "Abstract"
        parts.append(f"**{heading}**　{ir.meta.abstract}")
    if ir.meta.keywords:
        heading = "关键词" if ir.meta.language == "zh" else "Keywords"
        parts.append(f"**{heading}**：{', '.join(ir.meta.keywords)}")

    for section in ir.sections:
        parts.append(
            _render_section(
                section,
                numbering=numbering,
                figure_numbering=figure_numbering,
                language=ir.meta.language,
                asset_urls=asset_urls or {},
            )
        )

    if refs:
        heading = "参考文献" if ir.meta.language == "zh" else "References"
        parts.append(f"## {heading}")
        parts.append(
            _render_references(refs, by_key=by_key, numbering=numbering, style=citation_style)
        )
    return "\n\n".join(part for part in parts if part.strip()) + "\n"


def _render_section(
    section: Section,
    *,
    numbering: Mapping[str, int],
    figure_numbering: Mapping[str, int],
    language: str,
    asset_urls: Mapping[str, str],
) -> str:
    heading = _HEADING.get(section.level, "####")
    lines = [f"{heading} {section.title}"]
    for block in section.blocks:
        rendered = _render_block(
            block,
            numbering=numbering,
            figure_numbering=figure_numbering,
            language=language,
            asset_urls=asset_urls,
        )
        if rendered:
            lines.append(rendered)
    for warning in section.citation_warnings:
        # 编辑器可见的 R2 告警在预览里也保留，避免「静默剔除」。
        keys = ", ".join(warning.rejected_keys)
        lines.append(
            f"> ⚠️ {warning.message}（{keys}）"
            if language == "zh"
            else f"> ⚠️ {warning.message} ({keys})"
        )
    return "\n\n".join(lines)


def _render_block(
    block,
    *,
    numbering: Mapping[str, int],
    figure_numbering: Mapping[str, int],
    language: str,
    asset_urls: Mapping[str, str],
) -> str:
    if isinstance(block, ParagraphBlock):
        return "".join(
            _render_run(
                run,
                numbering=numbering,
                figure_numbering=figure_numbering,
                language=language,
            )
            for run in block.runs
        ).strip()
    if isinstance(block, EquationBlock):
        return f"$$\n{block.latex}\n$$"
    if isinstance(block, FigureBlock):
        caption = block.caption or ""
        alt = block.alt_text or caption
        source = asset_urls.get(block.asset_ref, block.asset_ref)
        figure_no = figure_numbering.get(block.label or "")
        prefix = "图" if language == "zh" else "Figure"
        legend = f"{prefix} {figure_no}　{caption}" if figure_no else caption
        return f"![{alt}]({source})\n\n*{legend}*" if legend else f"![{alt}]({source})"
    if isinstance(block, TableBlock):
        ref = block.source.ref or ""
        data = block.source.data or {}
        headers = [str(item) for item in data.get("headers") or []]
        rows = data.get("rows") or []
        if headers and rows:

            def cell(value: object) -> str:
                return str(value).replace("|", "\\|").replace("\n", " ")

            lines = [
                "| " + " | ".join(cell(item) for item in headers) + " |",
                "| " + " | ".join("---" for _ in headers) + " |",
            ]
            for row in rows:
                values = list(row)[: len(headers)] if isinstance(row, list) else [row]
                values += [""] * (len(headers) - len(values))
                lines.append("| " + " | ".join(cell(item) for item in values) + " |")
            caption = f"*{block.caption}*\n\n" if block.caption else ""
            return caption + "\n".join(lines)
        return f"*表：{block.caption}*（数据来源：{ref}）" if block.caption else f"*表*（{ref}）"
    if isinstance(block, AlgorithmBlock):
        return f"```\n{block.latex}\n```"
    if isinstance(block, TodoBlock):
        # 不编造实验数据：占位符在预览里必须醒目（设计 §4.4.2 红线）。
        return f"> **TODO**：{block.text}"
    if isinstance(block, ListBlock):
        lines = []
        for index, item in enumerate(block.items, start=1):
            bullet = f"{index}." if block.ordered else "-"
            body = "".join(
                _render_run(
                    run,
                    numbering=numbering,
                    figure_numbering=figure_numbering,
                    language=language,
                )
                for run in item.runs
            ).strip()
            lines.append(f"{bullet} {body}")
        return "\n".join(lines)
    return ""


def _render_run(
    run,
    *,
    numbering: Mapping[str, int],
    figure_numbering: Mapping[str, int],
    language: str,
) -> str:
    if isinstance(run, TextRun):
        text = run.v
        # 与 LaTeX 渲染器保持同一套语义：强调来自结构化 marks，不是正文里的星号。
        if "bold" in run.marks:
            text = f"**{text}**"
        if "italic" in run.marks:
            text = f"*{text}*"
        return text
    if isinstance(run, CiteRun):
        if not run.keys:
            return ""
        marks = []
        for key in run.keys:
            index = numbering.get(key)
            marks.append(f"[{index}]" if index else f"[{key}]")
        return " " + "".join(marks)
    if isinstance(run, MathInlineRun):
        return f"${run.v}$"
    if isinstance(run, XRefRun):
        index = figure_numbering.get(run.target)
        if index is None:
            return "图 ?" if language == "zh" else "Figure ?"
        return f"图 {index}" if language == "zh" else f"Figure {index}"
    return ""


def _render_references(
    refs: list[ReferenceMetadata],
    *,
    by_key: Mapping[str, ReferenceMetadata],
    numbering: Mapping[str, int],
    style: CitationStyle | str,
) -> str:
    lines: list[str] = []
    for key, index in sorted(numbering.items(), key=lambda item: item[1]):
        ref = by_key.get(key)
        if ref is None:
            continue
        lines.append(f"{index}. {format_scholarly_reference(ref, style=style)}")
    return "\n".join(lines)
