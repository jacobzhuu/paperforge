from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from paperforge_api.storage_migration import (
    export_storage,
    load_manifest,
    migrate_storage,
    restore_storage,
    write_source_manifest,
)
from storage import FilesystemObjectStore


def _source(root: Path) -> None:
    (root / "users/u/projects/p/exports").mkdir(parents=True)
    (root / "users/u/projects/p/exports/paper.pdf").write_bytes(b"%PDF-object")
    (root / "shared/oa").mkdir(parents=True)
    (root / "shared/oa/source.pdf").write_bytes(b"source-object")


def test_filesystem_migration_is_verified_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    manifest = tmp_path / "manifest.json"
    _source(source)
    entries = write_source_manifest(source, manifest)

    first = migrate_storage(
        source_root=source,
        manifest_path=manifest,
        destination=FilesystemObjectStore(str(destination)),
    )
    second = migrate_storage(
        source_root=source,
        manifest_path=manifest,
        destination=FilesystemObjectStore(str(destination)),
        verify_only=True,
    )

    assert first == {"copied": 2, "skipped": 0, "verified": 2}
    assert second == {"copied": 0, "skipped": 2, "verified": 2}
    assert [entry.key for entry in entries] == [
        "shared/oa/source.pdf",
        "users/u/projects/p/exports/paper.pdf",
    ]


def test_migration_rejects_extra_destination_key(tmp_path: Path) -> None:
    source = tmp_path / "source"
    manifest = tmp_path / "manifest.json"
    _source(source)
    write_source_manifest(source, manifest)
    destination = FilesystemObjectStore(str(tmp_path / "destination"))
    destination.put("extra.txt", b"extra")

    with pytest.raises(RuntimeError, match="destination/manifest key mismatch"):
        migrate_storage(
            source_root=source,
            manifest_path=manifest,
            destination=destination,
        )


def test_migration_rejects_source_checksum_drift(tmp_path: Path) -> None:
    source = tmp_path / "source"
    manifest = tmp_path / "manifest.json"
    _source(source)
    write_source_manifest(source, manifest)
    (source / "shared/oa/source.pdf").write_bytes(b"changed")

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        migrate_storage(
            source_root=source,
            manifest_path=manifest,
            destination=FilesystemObjectStore(str(tmp_path / "destination")),
        )


def test_export_and_restore_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    archive = tmp_path / "objects.tar.gz"
    manifest = tmp_path / "objects-manifest.json"
    _source(source)

    entries = export_storage(
        store=FilesystemObjectStore(str(source)),
        archive_path=archive,
        manifest_path=manifest,
    )
    result = restore_storage(
        archive_path=archive,
        manifest_path=manifest,
        destination=FilesystemObjectStore(str(destination)),
    )

    assert len(entries) == 2
    assert result == {"copied": 2, "skipped": 0, "verified": 2}
    assert (destination / "shared/oa/source.pdf").read_bytes() == b"source-object"
    assert restore_storage(
        archive_path=archive,
        manifest_path=manifest,
        destination=FilesystemObjectStore(str(destination)),
        verify_only=True,
    ) == {"copied": 0, "skipped": 2, "verified": 2}


def test_restore_rejects_unsafe_archive_member(tmp_path: Path) -> None:
    archive = tmp_path / "objects.tar.gz"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [{"key": "safe.txt", "size": 1, "sha256": "0" * 64}],
            }
        ),
        encoding="utf-8",
    )
    payload = tmp_path / "payload"
    payload.write_bytes(b"x")
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(payload, arcname="../escape")

    with pytest.raises(ValueError, match="unsafe object key"):
        restore_storage(
            archive_path=archive,
            manifest_path=manifest,
            destination=FilesystemObjectStore(str(tmp_path / "destination")),
        )


def test_manifest_rejects_duplicate_keys(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    row = {"key": "same", "size": 0, "sha256": "0" * 64}
    manifest.write_text(
        json.dumps({"version": 1, "entries": [row, row]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate object key"):
        load_manifest(manifest)
