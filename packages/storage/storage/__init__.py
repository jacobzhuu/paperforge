"""对象存储 seam（迁移自 DeepSearch storage/）：MinIO/S3 或本地文件系统后端。

存 OA 全文 PDF、用户素材、LaTeX 工程与 PDF 产物。API 与 worker 共用。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str: ...
    def get(self, key: str) -> bytes: ...


class FilesystemObjectStore:
    """本地文件系统后端（开发默认；生产切 MinIO）。"""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError(f"unsafe object key: {key}")
        return p

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()


def content_key(prefix: str, data: bytes, suffix: str = "") -> str:
    """内容寻址 object key：便于跨项目缓存复用（如 literature_card.source_hash）。"""
    digest = hashlib.sha256(data).hexdigest()
    return f"{prefix}/{digest}{suffix}"


__all__ = ["ObjectStore", "FilesystemObjectStore", "content_key"]
