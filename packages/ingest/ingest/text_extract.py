"""HTML / 纯文本 / Markdown 抽取。

收敛自 DeepSearch parsing/extractors.py 的 HTML 段（设计 §3.1）：保留
「剥 script/style/nav、按块级标签断段、还原实体、取 <title>」的确定性行为，
去掉 OSINT 专属的发布时间/作者/canonical 元数据抽取族。
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from ingest.types import ParsedContent

PLAIN_TEXT_MIME_TYPES = frozenset(
    {
        "application/x-env",
        "application/x-yaml",
        "application/yaml",
        "text/markdown",
        "text/plain",
        "text/x-yaml",
        "text/yaml",
        "text/csv",
    }
)
HTML_MIME_TYPES = frozenset({"text/html", "application/xhtml+xml"})

# 这些容器里的文本永远不是正文。
_DROPPED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
# 块级标签结束时断段，避免把整页压成一行。
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "li",
        "tr",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "figcaption",
        "td",
        "th",
    }
)


class _HtmlTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: str | None = None
        self._dropped_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED_TAGS:
            self._dropped_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_TAGS:
            self._dropped_depth = max(0, self._dropped_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._dropped_depth:
            return
        if self._in_title:
            self.title = (self.title or "") + data
            return
        if data.strip():
            self.parts.append(data)


def extract_html_content(*, content: bytes | str, mime_type: str = "text/html") -> ParsedContent:
    raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    parser = _HtmlTextParser()
    parser.feed(raw)
    parser.close()
    text = _normalize_blocks("".join(parser.parts))
    title = " ".join((parser.title or "").split()) or _derive_title(text)
    return ParsedContent(
        text=text,
        title=title or None,
        source_type="html",
        metadata={"mime_type": mime_type, "char_count": len(text)},
    )


def extract_plain_text_content(
    *,
    content: bytes | str,
    mime_type: str = "text/plain",
) -> ParsedContent:
    raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    text = _normalize_blocks(unescape(raw))
    return ParsedContent(
        text=text,
        title=_derive_title(text),
        source_type="text",
        metadata={"mime_type": mime_type, "char_count": len(text)},
    )


def _normalize_blocks(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.split("\n")]
    blocks = [line for line in lines if line]
    return "\n\n".join(blocks).strip()


def _derive_title(text: str) -> str | None:
    for line in text.split("\n"):
        candidate = re.sub(r"^#+\s*", "", line).strip()
        if candidate:
            return candidate[:300]
    return None
