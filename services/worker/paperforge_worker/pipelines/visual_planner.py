"""结构化视觉规划。

此前 `visual_plan` 是纯确定性的：AI 插图的提示词就是「A clean academic conceptual
illustration representing {论文标题}」。它不理解章节内容、构图目标和论文语境，于是
「投毒攻击」被画成毒药袋，学术信息被装饰图顶掉，英文安全敏感词组合还会触发
Cloudflare 的内容检查。

这里让规划器读正文结构后输出**结构化建议**：为什么要这张图、依据哪些章节、
插到哪里、画什么。三条硬约束：

1. 建议阶段绝不调用 ImageProvider——本模块只产出 proposal，没有任何生图调用；
2. 文本模型不可用 / 超时 / JSON 不合法时，回退到原有的确定性规划器，
   `visual_plan` 永远有产物（draft-first）；
3. 送进模型的是章节标题与摘要级文本，不整篇灌入。

注意数据流向：这一层**会**把正文摘要发给文本模型。生成确认框因此只能承诺
「不会发送给**图像**服务商」，不能笼统写「不会发送论文原文」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from llm_runtime import LLMRunner

#: 一次规划最多提这么多条，AI 插图另有更严的上限——插图是唯一会花钱的一类。
MAX_PROPOSALS = 6
MAX_AI_IMAGES = 2
#: 每节送进模型的正文字符上限。规划只需要知道「这节在讲什么」。
SECTION_EXCERPT_CHARS = 600

_SYSTEM_PROMPT = """You plan figures for an academic paper. Given the section structure and
excerpts, decide which figures genuinely help the reader. Output JSON only:
{
  "proposals": [
    {
      "kind": "chart" | "diagram" | "ai_image",
      "reason": "why this figure helps, referencing the section content",
      "source_section_keys": ["section keys the content came from"],
      "target_section_key": "where it should be inserted",
      "title": "short figure title",
      "caption": "figure caption",
      "alt_text": "accessibility description",
      "diagram": {
        "direction": "LR" | "TB",
        "nodes": [{"id": "n1", "label": "..."}],
        "edges": [{"source": "n1", "target": "n2", "label": "optional"}]
      },
      "ai_image": {
        "subject": "what the illustration is about",
        "composition": "how it is laid out",
        "elements": ["element 1", "element 2"]
      }
    }
  ]
}
Rules:
- Prefer a DIAGRAM whenever the content has steps, components or relations that can be
  expressed as nodes and edges. A diagram carries information; an illustration does not.
- Only propose "ai_image" when the idea genuinely cannot be drawn as nodes and edges.
  Never use an illustration as a substitute for academic information.
- For "ai_image", describe shapes and relations, not words: generated text is always
  garbled. Avoid security-sensitive word combinations (attack, poison, malicious) which
  trip image-service content filters; describe the mechanism abstractly instead.
- Do not propose charts here; the system derives those deterministically from uploaded data.
- target_section_key and source_section_keys MUST come from the given key list.
- At most 4 proposals, at most 2 of kind "ai_image"."""


@dataclass
class PlannedProposal:
    """一条经过校验的建议。`spec` 已是可直接入库的 VisualSpec dict。"""

    kind: str
    spec: dict[str, Any]
    title: str
    caption: str
    alt_text: str
    target_section_key: str | None
    reason: str | None = None
    source_section_keys: list[str] = field(default_factory=list)


@dataclass
class SectionBrief:
    """送进规划器的章节摘要。"""

    key: str
    title: str
    excerpt: str


async def plan_visuals(
    *,
    sections: list[SectionBrief],
    runner: LLMRunner | None,
    allow_ai_images: bool,
) -> tuple[list[PlannedProposal], str]:
    """返回 (建议列表, generator 标记)。

    `generator` 用于 checkpoint 与排查：`llm:<model>` 还是 `deterministic_fallback`。
    调用方拿到空列表时应继续用自己的确定性建议，本函数不负责兜底内容。
    """
    if runner is None or not runner.enabled or not sections:
        return [], "deterministic"

    allowed = {section.key for section in sections}
    outline = "\n\n".join(
        f"[{section.key}] {section.title}\n{section.excerpt[:SECTION_EXCERPT_CHARS]}"
        for section in sections
    )
    result = await runner.agenerate_json(
        "planner",
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=f"Section keys: {', '.join(sorted(allowed))}\n\nSections:\n{outline}",
        max_output_tokens=3000,
        temperature=0.2,
        metadata={"stage": "visual_plan"},
    )
    if not result.ok or not isinstance(result.value, dict):
        # 模型不可用不是错误路径：确定性规划器接手，用户照样拿到建议。
        return [], "deterministic_fallback"

    proposals = _normalize(
        result.value.get("proposals"),
        allowed=allowed,
        allow_ai_images=allow_ai_images,
    )
    if not proposals:
        return [], "deterministic_fallback"
    return proposals, f"llm:{result.model}"


def _normalize(
    raw: Any,
    *,
    allowed: set[str],
    allow_ai_images: bool,
) -> list[PlannedProposal]:
    if not isinstance(raw, list):
        return []
    planned: list[PlannedProposal] = []
    ai_images = 0
    for item in raw:
        if len(planned) >= MAX_PROPOSALS:
            break
        if not isinstance(item, dict):
            continue
        kind = _text(item.get("kind"))
        target = _text(item.get("target_section_key"))
        if target not in allowed:
            # 模型编出来的章节键会让批准时找不到目标章节，直接丢弃。
            continue
        caption = _text(item.get("caption"))
        alt_text = _text(item.get("alt_text")) or caption
        if not caption or not alt_text:
            # caption/alt_text 是批准接口的硬性要求，缺一条这建议就是死的。
            continue
        sources = [key for key in _as_list(item.get("source_section_keys")) if key in allowed]

        if kind == "diagram":
            spec = _diagram_spec(item.get("diagram"))
        elif kind == "ai_image":
            if not allow_ai_images or ai_images >= MAX_AI_IMAGES:
                continue
            spec = _ai_image_spec(item.get("ai_image"))
        else:
            # chart 由确定性路径从已上传的表格推导，模型不得凭空造数据图。
            continue
        if spec is None:
            continue
        if kind == "ai_image":
            ai_images += 1
        planned.append(
            PlannedProposal(
                kind=kind,
                spec=spec,
                title=_text(item.get("title")) or caption[:60],
                caption=caption,
                alt_text=alt_text,
                target_section_key=target,
                reason=_text(item.get("reason")) or None,
                source_section_keys=sources or [target],
            )
        )
    return planned


def _diagram_spec(raw: Any) -> dict[str, Any] | None:
    from visuals import DiagramSpec

    if not isinstance(raw, dict):
        return None
    nodes = []
    seen: set[str] = set()
    for item in _as_dicts(raw.get("nodes"))[:20]:
        node_id = _text(item.get("id"))
        label = _text(item.get("label"))
        if not node_id or not label or node_id in seen:
            continue
        seen.add(node_id)
        nodes.append({"id": node_id, "label": label[:80]})
    if len(nodes) < 2:
        return None
    edges = []
    for item in _as_dicts(raw.get("edges"))[:40]:
        source = _text(item.get("source"))
        target = _text(item.get("target"))
        # 悬空边会让 DiagramSpec 的 graph_is_closed 校验整条建议失败，
        # 与其丢掉整张图，不如丢掉那条边。
        if source not in seen or target not in seen:
            continue
        edge: dict[str, Any] = {"source": source, "target": target}
        label = _text(item.get("label"))
        if label:
            edge["label"] = label[:60]
        edges.append(edge)
    direction = _text(raw.get("direction")).upper()
    try:
        return DiagramSpec(
            direction="LR" if direction not in {"TB", "LR"} else direction,
            nodes=nodes,
            edges=edges,
            width="full" if len(nodes) > 5 else "column",
        ).model_dump(mode="json")
    except ValueError:
        return None


def _ai_image_spec(raw: Any) -> dict[str, Any] | None:
    from visuals import AIImageSemantics, AIImageSpec

    if not isinstance(raw, dict):
        return None
    subject = _text(raw.get("subject"))
    if len(subject) < 6:
        return None
    composition = _text(raw.get("composition"))
    elements = [_text(item)[:60] for item in _as_list(raw.get("elements"))][:6]
    elements = [item for item in elements if item]
    # prompt 仍然写满：语义层是新的表达方式，但下游（含历史数据、导出）都还
    # 依赖 prompt 字段，两者必须一致。
    prompt = ". ".join(
        part for part in (subject, composition, ", ".join(elements)) if part
    )
    try:
        return AIImageSpec(
            prompt=prompt if len(prompt) >= 10 else f"{subject} conceptual academic illustration",
            semantics=AIImageSemantics(
                subject=subject,
                composition=composition or None,
                elements=elements,
                text_policy="none",
                aspect_ratio="3:2",
            ),
        ).model_dump(mode="json")
    except ValueError:
        # 模型给的描述可能落在 AI 图禁区（量化表述、URL）。这是一条可选建议，
        # 不能因此让整个 visual_plan 失败。
        return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


def _as_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
