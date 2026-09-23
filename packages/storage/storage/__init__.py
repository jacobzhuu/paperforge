"""对象存储 seam（迁移自 DeepSearch storage/）：MinIO/S3 或本地文件系统后端。

存 OA 全文 PDF、用户素材、LaTeX 工程与 PDF 产物。API 与 worker 共用。
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
from typing import Any, Protocol


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def probe(self) -> None: ...


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

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def probe(self) -> None:
        """后端此刻是否还能收产物。构造函数建过目录，运行期它仍可能被卸载/只读。"""
        self.root.mkdir(parents=True, exist_ok=True)
        if not os.access(self.root, os.W_OK):
            raise RuntimeError(f"object store root is not writable: {self.root}")


class MinioObjectStore:
    """Private S3-compatible bucket. Keys are never exposed as public URLs."""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ) -> None:
        from minio import Minio
        from minio.error import S3Error

        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self.bucket = bucket
        if not self.client.bucket_exists(bucket):
            try:
                self.client.make_bucket(bucket)
            except S3Error as error:
                # Another API/worker can win the same first-use bucket creation.
                if error.code != "BucketAlreadyOwnedByYou":
                    raise
        # S3 buckets are private without a bucket policy. Remove any policy left by an
        # earlier development setup so authenticated API downloads remain the only path.
        try:
            self.client.delete_bucket_policy(bucket)
        except S3Error as error:
            if error.code not in {"NoSuchBucketPolicy", "NoSuchPolicy"}:
                raise

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        self.client.put_object(
            self.bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type or "application/octet-stream",
        )
        return key

    def get(self, key: str) -> bytes:
        response = self.client.get_object(self.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def delete(self, key: str) -> None:
        self.client.remove_object(self.bucket, key)

    def probe(self) -> None:
        """一次 HEAD 就能分辨「桶还在」与「服务不可达」，可放进容器健康检查。"""
        if not self.client.bucket_exists(self.bucket):
            raise RuntimeError(f"object store bucket is missing: {self.bucket}")


def make_object_store(settings: Any) -> ObjectStore:
    backend = str(getattr(settings, "storage_backend", "filesystem")).strip().lower()
    if backend == "filesystem":
        return FilesystemObjectStore(str(settings.storage_fs_root))
    if backend == "minio":
        return MinioObjectStore(
            endpoint=str(settings.minio_endpoint),
            access_key=str(settings.minio_access_key),
            secret_key=str(settings.minio_secret_key),
            bucket=str(settings.minio_bucket),
            secure=bool(settings.minio_secure),
        )
    raise ValueError(f"unsupported storage backend: {backend}")


def content_key(prefix: str, data: bytes, suffix: str = "") -> str:
    """内容寻址 object key：便于跨项目缓存复用（如 literature_card.source_hash）。"""
    digest = hashlib.sha256(data).hexdigest()
    return f"{prefix}/{digest}{suffix}"


__all__ = [
    "FilesystemObjectStore",
    "MinioObjectStore",
    "ObjectStore",
    "content_key",
    "make_object_store",
]
