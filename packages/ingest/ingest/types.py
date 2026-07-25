"""解析产物类型。

迁移自 DeepSearch parsing/extractors.py 的 ``ParsedContent``（设计 §3.1）；
去掉 OSINT 专属的 HTML 元数据抽取族，PaperForge 只需要文本 + 标题 + 定位元数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class UnsupportedMimeTypeError(Exception):
    def __init__(self, mime_type: str) -> None:
        super().__init__(f"unsupported mime type: {mime_type}")
        self.mime_type = mime_type


@dataclass(frozen=True)
class ParsedContent:
    text: str
    title: str | None
    source_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
