from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ChartKind = Literal["bar", "line", "scatter", "box", "heatmap"]
ChartAggregation = Literal["none", "mean", "median", "sum", "count"]
ChartSort = Literal["none", "asc", "desc"]
FigureWidth = Literal["column", "full"]

_REMOTE_OR_CODE = re.compile(
    r"(?:https?://|javascript:|<\s*script|```|\b(?:import|exec|eval|subprocess)\b)",
    re.IGNORECASE,
)
_AI_FORBIDDEN = re.compile(
    r"(?:坐标轴|结果曲线|准确率|精确数据|实验数据|混淆矩阵|散点图|柱状图|折线图|"
    r"accuracy\s*(?:plot|curve)|result\s*curve|axis|scatter\s*plot|bar\s*chart|"
    r"line\s*chart|confusion\s*matrix|precise\s*(?:data|device))",
    re.IGNORECASE,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChartFilter(StrictModel):
    column: str = Field(min_length=1, max_length=128)
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in"]
    value: str | float | int | list[str | float | int]

    @model_validator(mode="after")
    def operator_matches_value_shape(self) -> ChartFilter:
        if self.op == "in" and (not isinstance(self.value, list) or not self.value):
            raise ValueError("the in filter requires a non-empty value list")
        if self.op != "in" and isinstance(self.value, list):
            raise ValueError("only the in filter accepts a value list")
        return self


class ChartSpec(StrictModel):
    kind: Literal["chart"] = "chart"
    chart_type: ChartKind
    source_asset_ref: str = Field(pattern=r"^ua_[0-9a-fA-F]{8}$")
    x: str = Field(min_length=1, max_length=128)
    y: list[str] = Field(min_length=1, max_length=8)
    series: str | None = Field(default=None, max_length=128)
    error_lower: str | None = Field(default=None, max_length=128)
    error_upper: str | None = Field(default=None, max_length=128)
    x_label: str = Field(default="", max_length=160)
    y_label: str = Field(default="", max_length=160)
    unit: str = Field(default="", max_length=64)
    filters: list[ChartFilter] = Field(default_factory=list, max_length=8)
    aggregation: ChartAggregation = "none"
    sort: ChartSort = "none"
    width: FigureWidth = "column"
    palette: Literal["colorblind", "grayscale"] = "colorblind"

    @field_validator("x", "y", "series", "error_lower", "error_upper")
    @classmethod
    def reject_code_like_fields(cls, value):
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item and _REMOTE_OR_CODE.search(str(item)):
                raise ValueError("field names cannot contain URLs or executable content")
        return value

    @field_validator("x_label", "y_label", "unit")
    @classmethod
    def reject_code_like_labels(cls, value: str) -> str:
        """轴标签与单位同样过 `_REMOTE_OR_CODE`。

        这三个字段和列名一样会被渲染进 SVG/PDF，此前却是唯一绕过校验的自由文本：
        节点标签里写 `https://…` 会被拒，写进 y_label 却放行，同为展示文本却两套标准。
        （并非可利用漏洞——matplotlib 会做 XML 转义、前端用 `<img>` 而非内联 SVG——
        但校验口径不该按字段随意松紧。）
        """
        if value and _REMOTE_OR_CODE.search(value):
            raise ValueError("axis labels cannot contain URLs or executable content")
        return value

    @model_validator(mode="after")
    def chart_shape_is_valid(self) -> ChartSpec:
        if len(set(self.y)) != len(self.y):
            raise ValueError("y columns must be unique")
        if self.chart_type == "heatmap" and len(self.y) < 2:
            raise ValueError("heatmap requires at least two y columns")
        if bool(self.error_lower) != bool(self.error_upper):
            raise ValueError("error_lower and error_upper must be supplied together")
        if self.error_lower and len(self.y) != 1:
            raise ValueError("error bounds require exactly one y column")
        if self.error_lower and self.aggregation != "none":
            raise ValueError("error bounds cannot be combined with aggregation")
        return self


class DiagramNode(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=120)
    group: str | None = Field(default=None, max_length=64)
    shape: Literal["box", "rounded", "ellipse", "diamond"] = "rounded"

    @field_validator("label")
    @classmethod
    def reject_unsafe_label(cls, value: str) -> str:
        if _REMOTE_OR_CODE.search(value) or any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
            raise ValueError("diagram labels cannot contain URLs, scripts, or control characters")
        return value


class DiagramEdge(StrictModel):
    source: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    target: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    label: str = Field(default="", max_length=80)

    @field_validator("label")
    @classmethod
    def reject_unsafe_label(cls, value: str) -> str:
        if _REMOTE_OR_CODE.search(value):
            raise ValueError("edge labels cannot contain URLs or executable content")
        return value


class DiagramGroup(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)


class DiagramSpec(StrictModel):
    kind: Literal["diagram"] = "diagram"
    direction: Literal["TB", "LR"] = "TB"
    nodes: list[DiagramNode] = Field(min_length=1, max_length=30)
    edges: list[DiagramEdge] = Field(default_factory=list, max_length=60)
    groups: list[DiagramGroup] = Field(default_factory=list, max_length=12)
    width: FigureWidth = "column"

    @model_validator(mode="after")
    def graph_is_closed(self) -> DiagramSpec:
        node_ids = [node.id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("diagram node ids must be unique")
        group_ids = [group.id for group in self.groups]
        if len(set(group_ids)) != len(group_ids):
            raise ValueError("diagram group ids must be unique")
        known = set(node_ids)
        dangling = [
            edge for edge in self.edges if edge.source not in known or edge.target not in known
        ]
        if dangling:
            raise ValueError("diagram edges must reference existing nodes")
        known_groups = set(group_ids)
        if any(node.group and node.group not in known_groups for node in self.nodes):
            raise ValueError("diagram nodes must reference existing groups")
        return self


class AIImageSemantics(StrictModel):
    """提供商无关的语义描述。

    业务层保存「要画什么」，各 ImageProvider 适配器负责转成厂商请求。这样接入
    GPT Image / Gemini / ComfyUI 时，视觉工作台、PaperIR 与审核流程都不用改。

    全部可选：不填时 `AIImageSpec` 退回纯 `prompt`，历史 spec_json 原样可解析。
    `input_hash` 走语义投影（`db.repositories.visuals.semantic_projection`），
    未设定的字段不参与哈希，因此加这一层不会让旧建议被重复提出。
    """

    subject: str | None = Field(default=None, max_length=200)
    composition: str | None = Field(default=None, max_length=200)
    elements: list[str] = Field(default_factory=list, max_length=8)
    #: 学术插图里的文字几乎必然是伪中文/伪英文，默认一个字都不要。
    text_policy: Literal["none", "minimal"] = "none"
    aspect_ratio: str | None = Field(default=None, max_length=16)

    @field_validator("subject", "composition")
    @classmethod
    def conceptual_only_text(cls, value: str | None) -> str | None:
        return _reject_non_conceptual(value)

    @field_validator("elements")
    @classmethod
    def conceptual_only_elements(cls, value: list[str]) -> list[str]:
        # 校验必须覆盖新字段：否则 elements 就是一条绕过 AI 图禁区的旁路。
        for item in value:
            _reject_non_conceptual(item)
        return value


class AIImageSpec(StrictModel):
    kind: Literal["ai_image"] = "ai_image"
    prompt: str = Field(min_length=10, max_length=4000)
    size: Literal["1024x1024", "1536x1024", "1024x1536"] = "1536x1024"
    quality: Literal["low", "medium", "high"] = "medium"
    style: str = Field(default="clean academic conceptual illustration", max_length=160)
    width: FigureWidth = "full"
    semantics: AIImageSemantics | None = None

    @field_validator("prompt", "style")
    @classmethod
    def conceptual_only(cls, value: str) -> str:
        result = _reject_non_conceptual(value)
        assert result is not None  # noqa: S101 - 非空输入必得非空输出
        return result

    def render_prompt(self) -> str:
        """**实际会发送给图像服务商的那一句**。

        生成确认框展示的就是这个返回值——不能让界面自己再拼一遍，否则用户
        确认的文本和真正发出去的文本会悄悄分叉。
        """
        parts: list[str] = []
        semantics = self.semantics
        if semantics is not None and semantics.subject:
            parts.append(semantics.subject)
            if semantics.composition:
                parts.append(f"composition: {semantics.composition}")
            if semantics.elements:
                parts.append(f"elements: {', '.join(semantics.elements)}")
        else:
            parts.append(self.prompt)
        parts.append(f"Style: {self.style}")
        if semantics is None or semantics.text_policy == "none":
            parts.append("no text, no labels, no numerals")
        return ". ".join(part.strip().rstrip(".") for part in parts if part.strip()) + "."


def _reject_non_conceptual(value: str | None) -> str | None:
    if value is None:
        return None
    if _REMOTE_OR_CODE.search(value):
        raise ValueError("AI image prompts cannot contain URLs or executable content")
    if _AI_FORBIDDEN.search(value):
        raise ValueError("AI images are limited to conceptual illustrations")
    return value


VisualSpec = Annotated[ChartSpec | DiagramSpec | AIImageSpec, Field(discriminator="kind")]


def parse_visual_spec(payload: dict) -> ChartSpec | DiagramSpec | AIImageSpec:
    kind = payload.get("kind")
    model = {"chart": ChartSpec, "diagram": DiagramSpec, "ai_image": AIImageSpec}.get(kind)
    if model is None:
        raise ValueError(f"unsupported visual kind: {kind}")
    return model.model_validate(payload)
