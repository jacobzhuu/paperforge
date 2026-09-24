from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr, Field, model_validator

# PaperIR：结构化论文中间表示（方案 §4.5）。
# 继承旧系统「渲染器只消费 IR、不消费 LLM 原始输出」的原则，面向 LaTeX 重新设计。
# cite 是原子节点而非正文字符串 —— R2 校验与前端 chip 都建立在此之上；
# LLM 生成的自由 LaTeX 仅允许出现在 equation/algorithm 块（渲染期过白名单环境校验）。

CitationStyleName = Literal["gbt7714", "ieee", "apa", "author_year"]


# ---- 行内 run（段落内的原子片段）----

# 行内强调。刻意只有两种，且是**结构化标记**而不是 LLM 写的自由 LaTeX：
# 渲染器据此确定性展开 \textbf{} / \emph{}，正文内容照常转义。
# 这样编辑器可以提供加粗/斜体而不破坏「LLM 不书写 LaTeX」的边界（设计 §4.5）。
TextMark = Literal["bold", "italic"]


class TextRun(BaseModel):
    t: Literal["text"] = "text"
    v: str
    marks: list[TextMark] = Field(default_factory=list)


class CiteRun(BaseModel):
    t: Literal["cite"] = "cite"
    keys: list[str] = Field(default_factory=list)  # 必须 ⊆ 项目白名单（R2）
    # R6：句级引用可进一步绑定到 EvidenceUnit；渲染器仍只消费 keys。
    evidence_ids: list[str] = Field(default_factory=list)


class GroundingRun(BaseModel):
    """Invisible sentence-level provenance for user supplied assets."""

    t: Literal["grounding"] = "grounding"
    source_refs: list[str] = Field(default_factory=list)


class MathInlineRun(BaseModel):
    t: Literal["math_inline"] = "math_inline"
    v: str


class XRefRun(BaseModel):
    """对结构化图表标签的稳定交叉引用。"""

    t: Literal["xref"] = "xref"
    target: str
    kind: Literal["figure", "equation"] = "figure"


Run = TextRun | CiteRun | GroundingRun | MathInlineRun | XRefRun


# ---- 块级元素 ----
class ParagraphBlock(BaseModel):
    type: Literal["paragraph"] = "paragraph"
    runs: list[Run] = Field(default_factory=list)
    stance_summary: (
        Literal[
            "consistent",
            "conditional",
            "conflicting",
            "insufficient",
            "partial",
            "background",
        ]
        | None
    ) = None


class EquationBlock(BaseModel):
    type: Literal["equation"] = "equation"
    latex: str
    label: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    source_latex: str | None = None
    source_context: str | None = None
    explanation: str | None = None


class FigureBlock(BaseModel):
    type: Literal["figure"] = "figure"
    asset_ref: str
    caption: str = ""
    alt_text: str = ""
    label: str | None = None
    width: Literal["column", "full"] = "column"


class TableSource(BaseModel):
    kind: Literal["user_asset", "inline"]
    ref: str | None = None
    data: dict[str, object] | None = None


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


class ListItem(BaseModel):
    """列表项：与段落同构，因此项内同样可以有引用与行内公式。"""

    runs: list[Run] = Field(default_factory=list)


class ListBlock(BaseModel):
    """无序 / 有序列表。渲染为 itemize / enumerate。"""

    type: Literal["list"] = "list"
    ordered: bool = False
    items: list[ListItem] = Field(default_factory=list)


Block = (
    ParagraphBlock
    | EquationBlock
    | FigureBlock
    | TableBlock
    | AlgorithmBlock
    | TodoBlock
    | ListBlock
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
    appendix: bool = False
    blocks: list[Block] = Field(default_factory=list)
    citation_warnings: list[CitationWarning] = Field(default_factory=list)


class PaperAuthor(BaseModel):
    id: str
    name: str
    affiliations: list[str] = Field(default_factory=list)
    email: EmailStr | None = None
    orcid: str | None = None
    corresponding: bool = False

    @model_validator(mode="after")
    def corresponding_has_email(self) -> PaperAuthor:
        if self.corresponding and self.email is None:
            raise ValueError("a corresponding author must have an email address")
        return self


class PaperMeta(BaseModel):
    title: str
    authors: list[str] = Field(default_factory=list)
    author_details: list[PaperAuthor] = Field(default_factory=list)
    abstract: str = ""
    keywords: list[str] = Field(default_factory=list)
    language: Literal["zh", "en"] = "en"
    venue_template: str | None = None

    @model_validator(mode="after")
    def keep_legacy_authors_in_sync(self) -> PaperMeta:
        if self.author_details:
            self.authors = [author.name for author in self.author_details]
        elif self.authors:
            self.author_details = [
                PaperAuthor(id=f"legacy-{index + 1}", name=name)
                for index, name in enumerate(self.authors)
            ]
        return self


class Bibliography(BaseModel):
    style: CitationStyleName = "author_year"
    # 条目在渲染期从文献库注入（R3）；IR 不内嵌参考文献条目。
    entries_from: Literal["library"] = "library"


def _run_containers(block: Block) -> list[tuple[str, list[Run]]]:
    """块内所有承载 run 的容器，附带用于 violation path 的下标后缀。

    新增可含 run 的块类型时**必须**在这里登记，否则 R2 白名单校验会漏掉它。
    """
    if isinstance(block, ParagraphBlock):
        return [("", block.runs)]
    if isinstance(block, ListBlock):
        return [(f".items[{index}]", item.runs) for index, item in enumerate(block.items)]
    return []


class PaperIR(BaseModel):
    meta: PaperMeta
    sections: list[Section] = Field(default_factory=list)
    bibliography: Bibliography = Field(default_factory=Bibliography)

    @model_validator(mode="after")
    def labels_are_unique(self) -> PaperIR:
        labels: set[str] = set()
        for section in self.sections:
            for block in section.blocks:
                label = getattr(block, "label", None)
                if not label:
                    continue
                if label in labels:
                    raise ValueError(f"duplicate PaperIR label: {label}")
                labels.add(label)
        return self

    def collect_cite_keys(self) -> set[str]:
        """遍历所有承载 run 的块，收集用到的 cite keys（供 R2 白名单审计）。"""
        keys: set[str] = set()
        for section in self.sections:
            for block in section.blocks:
                for _, runs in _run_containers(block):
                    for run in runs:
                        if isinstance(run, CiteRun):
                            keys.update(run.keys)
        return keys

    def collect_asset_refs(self) -> set[str]:
        """收集图、表与句级接地使用的素材引用。"""
        refs: set[str] = set()
        for section in self.sections:
            for block in section.blocks:
                if isinstance(block, FigureBlock) and block.asset_ref:
                    refs.add(block.asset_ref)
                elif isinstance(block, TableBlock) and block.source.ref:
                    refs.add(block.source.ref)
                for _suffix, runs in _run_containers(block):
                    for run in runs:
                        if isinstance(run, GroundingRun):
                            refs.update(ref for ref in run.source_refs if ref)
        return refs

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
                # 列表项里的引用同样要过白名单——只查 ParagraphBlock 会给
                # R2 留一个绕过口子。
                for suffix, runs in _run_containers(block):
                    for run_index, run in enumerate(runs):
                        if not isinstance(run, CiteRun):
                            continue
                        rejected = tuple(key for key in run.keys if key not in allowed_cite_keys)
                        if not rejected:
                            continue
                        path = (
                            f"sections[{section_index}].blocks[{block_index}]"
                            f"{suffix}.runs[{run_index}].keys"
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
