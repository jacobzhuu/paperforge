"""Verified object-store backup and migration helpers.

The manifest is deliberately backend-neutral: object keys remain unchanged when moving from
the development filesystem backend to production MinIO. Every write is read back and hashed
before it is reported as successful; source objects are never deleted.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from storage import FilesystemObjectStore, MinioObjectStore, ObjectStore

MANIFEST_VERSION = 1


@dataclass(frozen=True)
class ObjectManifestEntry:
    key: str
    size: int
    sha256: str


def _safe_key(value: str) -> str:
    if "\\" in value:
        raise ValueError(f"unsafe object key in manifest: {value}")
    key = PurePosixPath(value)
    if key.is_absolute() or not key.parts or any(part in {"", ".", ".."} for part in key.parts):
        raise ValueError(f"unsafe object key in manifest: {value}")
    return key.as_posix()


def _entry(key: str, data: bytes) -> ObjectManifestEntry:
    return ObjectManifestEntry(
        key=_safe_key(key),
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _iter_filesystem(root: Path) -> Iterator[tuple[str, bytes]]:
    if not root.is_dir():
        raise ValueError(f"object source directory does not exist: {root}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"object source contains a symlink: {path}")
        if path.is_file():
            yield _safe_key(path.relative_to(root).as_posix()), path.read_bytes()


def _iter_store(store: ObjectStore) -> Iterator[tuple[str, bytes]]:
    if isinstance(store, FilesystemObjectStore):
        yield from _iter_filesystem(store.root)
        return
    if isinstance(store, MinioObjectStore):
        for item in store.client.list_objects(store.bucket, recursive=True):
            key = _safe_key(str(item.object_name))
            yield key, store.get(key)
        return
    raise TypeError(f"object-store export is not supported for {type(store).__name__}")


def write_source_manifest(source_root: Path, manifest_path: Path) -> list[ObjectManifestEntry]:
    entries = [_entry(key, data) for key, data in _iter_filesystem(source_root)]
    _write_manifest(manifest_path, entries)
    return entries


def _write_manifest(path: Path, entries: Iterable[ObjectManifestEntry]) -> None:
    entries_list = sorted(entries, key=lambda item: item.key)
    payload = {
        "version": MANIFEST_VERSION,
        "entries": [asdict(item) for item in entries_list],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_manifest(path: Path) -> list[ObjectManifestEntry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != MANIFEST_VERSION:
        raise ValueError("unsupported object manifest version")
    rows = payload.get("entries")
    if not isinstance(rows, list):
        raise ValueError("object manifest entries must be a list")
    entries: list[ObjectManifestEntry] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid object manifest entry")
        key = _safe_key(str(row.get("key") or ""))
        size = row.get("size")
        digest = str(row.get("sha256") or "").lower()
        if not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid object size for {key}")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"invalid SHA-256 for {key}")
        if key in seen:
            raise ValueError(f"duplicate object key in manifest: {key}")
        seen.add(key)
        entries.append(ObjectManifestEntry(key=key, size=size, sha256=digest))
    return sorted(entries, key=lambda item: item.key)


def _verify_bytes(entry: ObjectManifestEntry, data: bytes, *, location: str) -> None:
    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise RuntimeError(f"object checksum mismatch at {location}: {entry.key}")


def _get_optional(store: ObjectStore, key: str) -> bytes | None:
    try:
        return store.get(key)
    except FileNotFoundError:
        return None
    except Exception as error:
        if getattr(error, "code", None) in {"NoSuchKey", "NoSuchObject", "NoSuchFile"}:
            return None
        raise


def storage_object_count(store: ObjectStore) -> int:
    return sum(1 for _key, _data in _iter_store(store))


def _verify_destination_keys(store: ObjectStore, expected_keys: set[str]) -> None:
    actual_keys = {key for key, _data in _iter_store(store)}
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        raise RuntimeError(f"destination/manifest key mismatch; missing={missing}; extra={extra}")


def migrate_storage(
    *,
    source_root: Path,
    manifest_path: Path,
    destination: ObjectStore,
    dry_run: bool = False,
    verify_only: bool = False,
) -> dict[str, int]:
    if dry_run and verify_only:
        raise ValueError("--dry-run and --verify-only are mutually exclusive")
    entries = load_manifest(manifest_path)
    source = {key: data for key, data in _iter_filesystem(source_root)}
    expected_keys = {item.key for item in entries}
    if set(source) != expected_keys:
        missing = sorted(expected_keys - set(source))
        extra = sorted(set(source) - expected_keys)
        raise RuntimeError(f"object source/manifest key mismatch; missing={missing}; extra={extra}")

    copied = 0
    skipped = 0
    verified = 0
    for item in entries:
        data = source[item.key]
        _verify_bytes(item, data, location="source")
        if dry_run:
            verified += 1
            continue
        current = _get_optional(destination, item.key)
        if current is not None:
            try:
                _verify_bytes(item, current, location="destination")
            except RuntimeError:
                if verify_only:
                    raise
            else:
                skipped += 1
                verified += 1
                continue
        if verify_only:
            raise RuntimeError(f"object missing from destination: {item.key}")
        destination.put(item.key, data)
        written = destination.get(item.key)
        _verify_bytes(item, written, location="destination")
        copied += 1
        verified += 1
    if not dry_run:
        _verify_destination_keys(destination, expected_keys)
    return {"copied": copied, "skipped": skipped, "verified": verified}


def export_storage(
    *, store: ObjectStore, archive_path: Path, manifest_path: Path
) -> list[ObjectManifestEntry]:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_name(f".{archive_path.name}.tmp")
    entries: list[ObjectManifestEntry] = []
    try:
        with tarfile.open(temporary, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            for key, data in _iter_store(store):
                item = _entry(key, data)
                info = tarfile.TarInfo(name=item.key)
                info.size = item.size
                info.mode = 0o600
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                archive.addfile(info, io.BytesIO(data))
                entries.append(item)
        temporary.replace(archive_path)
        _write_manifest(manifest_path, entries)
    finally:
        temporary.unlink(missing_ok=True)
    return sorted(entries, key=lambda item: item.key)


def restore_storage(
    *,
    archive_path: Path,
    manifest_path: Path,
    destination: ObjectStore,
    dry_run: bool = False,
    verify_only: bool = False,
) -> dict[str, int]:
    entries = load_manifest(manifest_path)
    expected = {item.key: item for item in entries}
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        member_keys: set[str] = set()
        payloads: dict[str, bytes] = {}
        for member in members:
            key = _safe_key(member.name)
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError(f"object archive contains a non-file entry: {key}")
            if key in member_keys:
                raise ValueError(f"object archive contains a duplicate key: {key}")
            member_keys.add(key)
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"object archive member cannot be read: {key}")
            payloads[key] = handle.read()
    if member_keys != set(expected):
        missing = sorted(set(expected) - member_keys)
        extra = sorted(member_keys - set(expected))
        raise RuntimeError(
            f"object archive/manifest key mismatch; missing={missing}; extra={extra}"
        )

    copied = 0
    skipped = 0
    verified = 0
    for key in sorted(expected):
        item = expected[key]
        data = payloads[key]
        _verify_bytes(item, data, location="archive")
        if dry_run:
            verified += 1
            continue
        current = _get_optional(destination, key)
        if current is not None:
            try:
                _verify_bytes(item, current, location="destination")
            except RuntimeError:
                if verify_only:
                    raise
            else:
                skipped += 1
                verified += 1
                continue
        if verify_only:
            raise RuntimeError(f"object missing from destination: {key}")
        destination.put(key, data)
        _verify_bytes(item, destination.get(key), location="destination")
        copied += 1
        verified += 1
    if not dry_run:
        _verify_destination_keys(destination, set(expected))
    return {"copied": copied, "skipped": skipped, "verified": verified}


def configured_store() -> ObjectStore:
    # Local import prevents the administration module's settings cache from affecting tests.
    from paperforge_api.config import get_settings

    return make_configured_store(get_settings())


def make_configured_store(settings: Any) -> ObjectStore:
    from storage import make_object_store

    return make_object_store(settings)


__all__ = [
    "ObjectManifestEntry",
    "configured_store",
    "export_storage",
    "load_manifest",
    "migrate_storage",
    "restore_storage",
    "storage_object_count",
    "write_source_manifest",
]
