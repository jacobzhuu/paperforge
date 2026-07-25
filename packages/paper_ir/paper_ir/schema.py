from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# PaperIR：结构化论文中间表示（方案 §4.5）。
# 继承旧系统「渲染器只消费 IR、不消费 LLM 原始输出」的原则，面向 LaTeX 重新设计。
# cite 是原子节点而非正文字符串 —— R2 校验与前端 chip 都建立在此之上；
# LLM 生成的自由 LaTeX 仅允许出现在 equation/algorithm 块（渲染期过白名单环境校验）。

CitationStyleName = Literal["gbt7714", "ieee", "apa", "author_year"]


# ---- 行内 run（段落内的原子片段）----
class TextRun(BaseModel):
    t: Literal["text"] = "text"
    v: str


class CiteRun(BaseModel):
    t: Literal["cite"] = "cite"
    keys: list[str] = Field(default_factory=list)  # 必须 ⊆ 项目白名单（R2）


class MathInlineRun(BaseModel):
    t: Literal["math_inline"] = "math_inline"
    v: str


Run = TextRun | CiteRun | MathInlineRun


# ---- 块级元素 ----
class ParagraphBlock(BaseModel):
    type: Literal["paragraph"] = "paragraph"
    runs: list[Run] = Field(default_factory=list)


class EquationBlock(BaseModel):
    type: Literal["equation"] = "equation"
    latex: str
    label: str | None = None


class FigureBlock(BaseModel):
    type: Literal["figure"] = "figure"
    asset_ref: str
    caption: str = ""
    label: str | None = None


class TableSource(BaseModel):
    kind: Literal["user_asset", "inline"]
    ref: str | None = None


class TableBlock(BaseModel):
    type: Literal["table"] = "table"
    source: TableSource
    caption: str = ""
    label: str | None = None


class AlgorithmBlock(BaseModel):
    type: Literal["algorithm"] = "algorithm"
    latex: str
    label: str | None = None


class TodoBlock(BaseModel):
    """实验结果占位（纯生成模式）：不编造数据，正文明确标注待补充。"""

    type: Literal["todo"] = "todo"
    text: str = "待补充实验数据"


Block = (
    ParagraphBlock
    | EquationBlock
    | FigureBlock
    | TableBlock
    | AlgorithmBlock
    | TodoBlock
)


# ---- 结构 ----
class CitationWarning(BaseModel):
    """Editor-visible marker created when the second R2 pass removes citations."""

    path: str
    rejected_keys: tuple[str, ...]
    message: str = "引用已移除：引用键不在项目写作白名单中"


class PaperIRCiteKeyViolation(BaseModel):
    path: str
    rejected_keys: tuple[str, ...]


class Section(BaseModel):
    key: str
    level: int = 1
    title: str
    blocks: list[Block] = Field(default_factory=list)
    citation_warnings: list[CitationWarning] = Field(default_factory=list)


class PaperMeta(BaseModel):
    title: str
    authors: list[str] = Field(default_factory=list)
    abstract: str = ""
    keywords: list[str] = Field(default_factory=list)
    language: Literal["zh", "en"] = "en"
    venue_template: str | None = None


class Bibliography(BaseModel):
    style: CitationStyleName = "author_year"
    # 条目在渲染期从文献库注入（R3）；IR 不内嵌参考文献条目。
    entries_from: Literal["library"] = "library"


class PaperIR(BaseModel):
    meta: PaperMeta
    sections: list[Section] = Field(default_factory=list)
    bibliography: Bibliography = Field(default_factory=Bibliography)

    def collect_cite_keys(self) -> set[str]:
        """遍历所有段落，收集用到的 cite keys（供 R2 白名单审计）。"""
        keys: set[str] = set()
        for section in self.sections:
            for block in section.blocks:
                if isinstance(block, ParagraphBlock):
                    for run in block.runs:
                        if isinstance(run, CiteRun):
                            keys.update(run.keys)
        return keys

    def enforce_cite_key_whitelist(
        self,
        allowed_cite_keys: set[str],
        *,
        strip: bool = False,
    ) -> list[PaperIRCiteKeyViolation]:
        """R2's typed-IR guard, run after parsing structured LLM output.

        The first pass uses ``strip=False`` and feeds the returned violations to
        a rewrite prompt. The second pass uses ``strip=True``; rejected keys are
        removed and editor-visible warnings are persisted in the affected section.
        """
        violations: list[PaperIRCiteKeyViolation] = []
        for section_index, section in enumerate(self.sections):
            for block_index, block in enumerate(section.blocks):
                if not isinstance(block, ParagraphBlock):
                    continue
                for run_index, run in enumerate(block.runs):
                    if not isinstance(run, CiteRun):
                        continue
                    rejected = tuple(key for key in run.keys if key not in allowed_cite_keys)
                    if not rejected:
                        continue
                    path = (
                        f"sections[{section_index}].blocks[{block_index}]"
                        f".runs[{run_index}].keys"
                    )
                    violation = PaperIRCiteKeyViolation(
                        path=path,
                        rejected_keys=rejected,
                    )
                    violations.append(violation)
                    if strip:
                        run.keys = [key for key in run.keys if key in allowed_cite_keys]
                        section.citation_warnings.append(
                            CitationWarning(
                                path=path,
                                rejected_keys=rejected,
                            )
                        )
        return violations
