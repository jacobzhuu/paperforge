"""JATS/NLM XML 学术全文解析器。

只使用标准库 XML 解析，不解析外部实体、不加载网络资源。输出正文、章节树以及
表格/图注/公式对象的字符级定位，供 EvidenceUnit 回放。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from ingest.document_extractors import DocumentParseError, mime_policy_metadata
from ingest.types import ParsedContent


@dataclass
class _TextBuilder:
    parts: list[str] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    objects: list[dict[str, Any]] = field(default_factory=list)
    length: int = 0

    def append(
        self,
        text: str,
        *,
        section_path: str | None,
        object_ref: str | None = None,
        object_kind: str | None = None,
    ) -> None:
        cleaned = _clean_text(text)
        if not cleaned:
            return
        if self.parts:
            self.length += 2
        start = self.length
        self.parts.append(cleaned)
        self.length += len(cleaned)
        segment: dict[str, Any] = {
            "format": "jats",
            "char_start": start,
            "char_end": self.length,
            "page_locator_reliable": False,
        }
        if section_path:
            segment["section_title"] = section_path
            segment["section_path"] = section_path
        if object_ref:
            segment["object_ref"] = object_ref
            segment["object_kind"] = object_kind
            self.objects.append(
                {
                    "object_ref": object_ref,
                    "kind": object_kind,
                    "text": cleaned,
                    "char_start": start,
                    "char_end": self.length,
                    "section_path": section_path,
                }
            )
        self.segments.append(segment)

    @property
    def text(self) -> str:
        return "\n\n".join(self.parts)


def extract_jats_content(
    *,
    content: bytes,
    mime_type: str = "application/xml",
) -> ParsedContent:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise DocumentParseError(f"jats_xml_parse_error:{error}") from error
    # PMC OAI-PMH 把 JATS article 包在 GetRecord/metadata 中；直接定位后代，
    # 不把传输层包装误判为非 JATS。
    article = root if _local_name(root.tag) == "article" else _first_descendant(root, "article")
    if article is None:
        raise DocumentParseError("jats_article_missing")

    title_node = _first_descendant(article, "article-title")
    title = _node_text(title_node) or None
    builder = _TextBuilder()
    abstract = _direct_or_descendant(article, "abstract")
    if abstract is not None:
        builder.append(_node_text(abstract), section_path="Abstract")
    body = _direct_or_descendant(article, "body")
    if body is not None:
        _walk_jats(body, builder=builder, section_path=())
    if not builder.text:
        raise DocumentParseError("jats_body_empty")

    license_node = _first_descendant(article, "license")
    license_text = _node_text(license_node) or None
    return ParsedContent(
        text=builder.text,
        title=title,
        source_type="jats_article",
        metadata={
            **mime_policy_metadata(mime_type),
            "extractor": "jats_v1",
            "parser_status": "success",
            "parser_kind": "jats",
            "content_type": mime_type,
            "text_length": len(builder.text),
            "page_locator_reliable": False,
            "structure_segments": builder.segments,
            "structured_objects": builder.objects,
            "license": license_text,
        },
    )


def _walk_jats(
    node: ET.Element,
    *,
    builder: _TextBuilder,
    section_path: tuple[str, ...],
) -> None:
    tag = _local_name(node.tag)
    if tag == "sec":
        title_node = next(
            (child for child in node if _local_name(child.tag) == "title"),
            None,
        )
        title = _node_text(title_node) or "Untitled section"
        path = (*section_path, title)
        builder.append(title, section_path=" / ".join(path))
        for child in node:
            if child is not title_node:
                _walk_jats(child, builder=builder, section_path=path)
        return
    if tag == "p":
        builder.append(_node_text(node), section_path=" / ".join(section_path) or None)
        return
    if tag == "table-wrap":
        object_ref = _object_ref(node, prefix="table")
        builder.append(
            _jats_table_text(node),
            section_path=" / ".join(section_path) or None,
            object_ref=object_ref,
            object_kind="table",
        )
        return
    if tag == "fig":
        object_ref = _object_ref(node, prefix="fig")
        caption = _first_descendant(node, "caption")
        label = _first_descendant(node, "label")
        builder.append(
            " ".join(part for part in (_node_text(label), _node_text(caption)) if part),
            section_path=" / ".join(section_path) or None,
            object_ref=object_ref,
            object_kind="figure",
        )
        return
    if tag in {"disp-formula", "inline-formula"}:
        object_ref = _object_ref(node, prefix="eq")
        tex = _node_text(_first_descendant(node, "tex-math"))
        builder.append(
            f"$$\n{tex}\n$$" if tex else _node_text(node),
            section_path=" / ".join(section_path) or None,
            object_ref=object_ref,
            object_kind="equation",
        )
        return
    if tag in {"supplementary-material", "app"}:
        object_ref = _object_ref(node, prefix="supp")
        builder.append(
            _node_text(node),
            section_path=" / ".join(section_path) or None,
            object_ref=object_ref,
            object_kind="supplement",
        )
        return
    if tag in {"ref-list", "ack", "fn-group"}:
        return
    for child in node:
        _walk_jats(child, builder=builder, section_path=section_path)


def _jats_table_text(node: ET.Element) -> str:
    label = _node_text(_first_descendant(node, "label"))
    caption = _node_text(_first_descendant(node, "caption"))
    table = _first_descendant(node, "table")
    if table is None:
        return " ".join(part for part in (label, caption, _node_text(node)) if part)
    rows: list[list[str]] = []
    header_index = 0
    for tr in (item for item in table.iter() if _local_name(item.tag) == "tr"):
        cells = [_node_text(cell) for cell in tr if _local_name(cell.tag) in {"th", "td"}]
        if cells:
            if any(_local_name(cell.tag) == "th" for cell in tr):
                header_index = len(rows)
            rows.append(cells)
    if not rows:
        return " ".join(part for part in (label, caption, _node_text(table)) if part)
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized.pop(header_index) if normalized else [""] * width
    if not any(header):
        header = [f"column_{index + 1}" for index in range(width)]
    markdown = [
        "| " + " | ".join(_escape_table_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
        *["| " + " | ".join(_escape_table_cell(cell) for cell in row) + " |" for row in normalized],
    ]
    heading = " ".join(part for part in (label, caption) if part)
    return f"{heading}\n" + "\n".join(markdown) if heading else "\n".join(markdown)


def _object_ref(node: ET.Element, *, prefix: str) -> str:
    raw_id = str(node.attrib.get("id") or "").strip()
    label = _node_text(_first_descendant(node, "label"))
    value = raw_id or re.sub(r"\s+", "-", label.casefold()) or "unlabeled"
    return f"{prefix}:{value}"[:64]


def _first_descendant(node: ET.Element | None, tag: str) -> ET.Element | None:
    if node is None:
        return None
    return next((item for item in node.iter() if _local_name(item.tag) == tag), None)


def _direct_or_descendant(node: ET.Element, tag: str) -> ET.Element | None:
    direct = next(
        (child for child in node if _local_name(child.tag) == tag),
        None,
    )
    return direct if direct is not None else _first_descendant(node, tag)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _node_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    if _local_name(node.tag) in {"inline-formula", "disp-formula"}:
        tex = _first_descendant(node, "tex-math")
        if tex is not None:
            return "$" + _clean_text(" ".join(tex.itertext())) + "$"
    parts = [node.text or ""]
    for child in node:
        parts.extend((_node_text(child), child.tail or ""))
    return _clean_text(" ".join(parts))


def _clean_text(value: str) -> str:
    return " ".join((value or "").split())


def _escape_table_cell(value: str) -> str:
    return _clean_text(value).replace("|", r"\|")


__all__ = ["extract_jats_content"]
