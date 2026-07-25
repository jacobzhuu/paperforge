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
    MathInlineRun,
    PaperIR,
    ParagraphBlock,
    Section,
    TableBlock,
    TextRun,
    TodoBlock,
)

_HEADING = {1: "##", 2: "###", 3: "####"}


def render_markdown(
    ir: PaperIR,
    *,
    references: Iterable[ReferenceMetadata] = (),
    style: CitationStyle | str | None = None,
) -> str:
    """渲染整篇 Markdown：标题 + 摘要 + 章节 + 参考文献。"""
    refs = list(references)
    numbering = {ref.bibtex_key: index + 1 for index, ref in enumerate(refs) if ref.bibtex_key}
    by_key = {ref.bibtex_key: ref for ref in refs if ref.bibtex_key}
    citation_style = style or ir.bibliography.style

    parts: list[str] = [f"# {ir.meta.title}".rstrip()]
    if ir.meta.authors:
        parts.append("*" + ", ".join(ir.meta.authors) + "*")
    if ir.meta.abstract:
        heading = "摘要" if ir.meta.language == "zh" else "Abstract"
        parts.append(f"**{heading}**　{ir.meta.abstract}")
    if ir.meta.keywords:
        heading = "关键词" if ir.meta.language == "zh" else "Keywords"
        parts.append(f"**{heading}**：{', '.join(ir.meta.keywords)}")

    for section in ir.sections:
        parts.append(_render_section(section, numbering=numbering, language=ir.meta.language))

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
    language: str,
) -> str:
    heading = _HEADING.get(section.level, "####")
    lines = [f"{heading} {section.title}"]
    for block in section.blocks:
        rendered = _render_block(block, numbering=numbering)
        if rendered:
            lines.append(rendered)
    for warning in section.citation_warnings:
        # 编辑器可见的 R2 告警在预览里也保留，避免「静默剔除」。
        keys = ", ".join(warning.rejected_keys)
        lines.append(f"> ⚠️ {warning.message}（{keys}）" if language == "zh" else
                     f"> ⚠️ {warning.message} ({keys})")
    return "\n\n".join(lines)


def _render_block(block, *, numbering: Mapping[str, int]) -> str:
    if isinstance(block, ParagraphBlock):
        return "".join(_render_run(run, numbering=numbering) for run in block.runs).strip()
    if isinstance(block, EquationBlock):
        return f"$$\n{block.latex}\n$$"
    if isinstance(block, FigureBlock):
        caption = block.caption or ""
        return f"![{caption}]({block.asset_ref})\n\n*{caption}*" if caption else (
            f"![]({block.asset_ref})"
        )
    if isinstance(block, TableBlock):
        ref = block.source.ref or ""
        return f"*表：{block.caption}*（数据来源：{ref}）" if block.caption else f"*表*（{ref}）"
    if isinstance(block, AlgorithmBlock):
        return f"```\n{block.latex}\n```"
    if isinstance(block, TodoBlock):
        # 不编造实验数据：占位符在预览里必须醒目（设计 §4.4.2 红线）。
        return f"> **TODO**：{block.text}"
    return ""


def _render_run(run, *, numbering: Mapping[str, int]) -> str:
    if isinstance(run, TextRun):
        return run.v
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
