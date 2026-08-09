from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import hashlib
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from db import create_user, get_user_by_email, list_purgeable_projects, purge_project
from db.models.auth import AppUser
from db.models.library import DocumentFile, LiteraturePdfUpload
from db.models.paper import ExportArtifact, PaperProject, UserAsset, VisualAsset
from db.session import make_engine, make_session_factory
from sqlalchemy import select, update
from storage import make_object_store

from paperforge_api.auth_service import hash_password
from paperforge_api.config import get_settings
from paperforge_api.storage_migration import (
    export_storage,
    migrate_storage,
    restore_storage,
    storage_object_count,
    write_source_manifest,
)

LEGACY_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperForge account administration")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser(
        "bootstrap-admin", help="create/activate the first administrator"
    )
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--display-name", default="PaperForge administrator")
    bootstrap.add_argument("--claim-legacy", action="store_true")
    bootstrap.add_argument("--project-map", type=Path)
    bootstrap.add_argument("--migrate-objects", action="store_true")
    bootstrap.add_argument(
        "--password-stdin",
        action="store_true",
        help="read one password line from stdin (for secret-managed automation)",
    )
    migrate = commands.add_parser("migrate-objects", help="copy legacy keys into tenant namespaces")
    migrate.add_argument("--dry-run", action="store_true")
    manifest = commands.add_parser(
        "storage-manifest", help="create a SHA-256 manifest for a filesystem object tree"
    )
    manifest.add_argument("--source-root", type=Path, required=True)
    manifest.add_argument("--manifest", type=Path, required=True)
    export = commands.add_parser(
        "export-storage", help="export the configured object store to a verified archive"
    )
    export.add_argument("--archive", type=Path, required=True)
    export.add_argument("--manifest", type=Path, required=True)
    migrate_storage_parser = commands.add_parser(
        "migrate-storage", help="copy a filesystem object tree into the configured object store"
    )
    migrate_storage_parser.add_argument("--source-root", type=Path, required=True)
    migrate_storage_parser.add_argument("--manifest", type=Path, required=True)
    migrate_storage_parser.add_argument("--dry-run", action="store_true")
    migrate_storage_parser.add_argument("--verify-only", action="store_true")
    restore = commands.add_parser(
        "restore-storage", help="restore a verified archive into the configured object store"
    )
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument("--verify-only", action="store_true")
    storage_count = commands.add_parser(
        "storage-count", help="count objects in the configured object store"
    )
    storage_count.add_argument("--require-empty", action="store_true")
    purge = commands.add_parser(
        "purge-projects",
        help="permanently delete soft-deleted projects past their retention window",
    )
    purge.add_argument(
        "--days",
        type=int,
        default=30,
        help="retention window in days; only projects deleted before it are purged",
    )
    purge.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "bootstrap-admin":
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
            if not password:
                raise SystemExit("no password was received on stdin")
        else:
            password = getpass.getpass("New administrator password: ")
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                raise SystemExit("passwords do not match")
        password_hash = hash_password(password)
        asyncio.run(_bootstrap(args, password_hash))
    elif args.command == "migrate-objects":
        asyncio.run(_migrate_objects(dry_run=args.dry_run))
    elif args.command == "purge-projects":
        asyncio.run(_purge_projects(days=args.days, dry_run=args.dry_run))
    elif args.command == "storage-manifest":
        entries = write_source_manifest(args.source_root, args.manifest)
        print(f"object manifest created: {len(entries)} objects")
    elif args.command == "export-storage":
        entries = export_storage(
            store=make_object_store(get_settings()),
            archive_path=args.archive,
            manifest_path=args.manifest,
        )
        print(f"object storage exported and verified: {len(entries)} objects")
    elif args.command == "migrate-storage":
        result = migrate_storage(
            source_root=args.source_root,
            manifest_path=args.manifest,
            destination=make_object_store(get_settings()),
            dry_run=args.dry_run,
            verify_only=args.verify_only,
        )
        print(_storage_result("migration", result))
    elif args.command == "restore-storage":
        result = restore_storage(
            archive_path=args.archive,
            manifest_path=args.manifest,
            destination=make_object_store(get_settings()),
            dry_run=args.dry_run,
            verify_only=args.verify_only,
        )
        print(_storage_result("restore", result))
    else:
        count = storage_object_count(make_object_store(get_settings()))
        print(count)
        if args.require_empty and count:
            raise SystemExit("configured object store is not empty")


def _storage_result(operation: str, result: dict[str, int]) -> str:
    return (
        f"object storage {operation}: copied={result['copied']}; "
        f"already valid={result['skipped']}; verified={result['verified']}"
    )


async def _bootstrap(args: argparse.Namespace, password_hash: str) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            placeholder = await session.get(AppUser, LEGACY_USER_ID)
            user = await get_user_by_email(session, args.email)
            if user is None and placeholder is not None and placeholder.status == "disabled":
                user = placeholder
                user.email = args.email.strip().lower()
                user.display_name = args.display_name.strip() or None
                user.password_hash = password_hash
                user.status = "active"
                from datetime import UTC, datetime

                user.email_verified_at = datetime.now(UTC)
            elif user is None:
                user = await create_user(
                    session,
                    email=args.email,
                    password_hash=password_hash,
                    display_name=args.display_name,
                    verified=True,
                )
            else:
                from datetime import UTC, datetime

                user.password_hash = password_hash
                user.display_name = args.display_name.strip() or user.display_name
                user.status = "active"
                user.email_verified_at = user.email_verified_at or datetime.now(UTC)

            claimed = 0
            if args.claim_legacy and user.id != LEGACY_USER_ID:
                result = await session.execute(
                    update(PaperProject)
                    .where(PaperProject.owner_id == LEGACY_USER_ID)
                    .values(owner_id=user.id)
                )
                claimed += int(result.rowcount or 0)
            if args.project_map:
                claimed += await _apply_project_map(session, args.project_map)
            await session.commit()
            print(f"administrator ready: {user.email}; projects assigned: {claimed}")
        if args.migrate_objects:
            await _migrate_objects(dry_run=False)
    finally:
        await engine.dispose()


async def _apply_project_map(session: Any, path: Path) -> int:
    assigned = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            project_id = uuid.UUID((row.get("project_id") or "").strip())
            email = (row.get("email") or "").strip()
            user = await get_user_by_email(session, email)
            if user is None or user.status != "active":
                raise ValueError(f"active user not found for project mapping: {email}")
            result = await session.execute(
                update(PaperProject).where(PaperProject.id == project_id).values(owner_id=user.id)
            )
            if not result.rowcount:
                raise ValueError(f"project not found for mapping: {project_id}")
            assigned += 1
    return assigned


async def _migrate_objects(*, dry_run: bool) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    store = make_object_store(settings)
    copied = 0
    skipped = 0
    try:
        async with factory() as session:
            assets = (
                await session.execute(
                    select(UserAsset, PaperProject).join(
                        PaperProject, PaperProject.id == UserAsset.project_id
                    )
                )
            ).all()
            for asset, project in assets:
                changed, key = _private_key(asset.object_key, project)
                if changed:
                    copied += await _copy_verified(store, asset.object_key, key, dry_run=dry_run)
                    if not dry_run:
                        asset.object_key = key
                else:
                    skipped += 1

            exports = (
                await session.execute(
                    select(ExportArtifact, PaperProject).join(
                        PaperProject, PaperProject.id == ExportArtifact.project_id
                    )
                )
            ).all()
            for artifact, project in exports:
                for field in ("object_key", "compile_log_key"):
                    old_key = getattr(artifact, field)
                    changed, key = _private_key(old_key, project)
                    if changed:
                        copied += await _copy_verified(store, old_key, key, dry_run=dry_run)
                        if not dry_run:
                            setattr(artifact, field, key)
                    else:
                        skipped += 1

            visuals = (
                await session.execute(
                    select(VisualAsset, PaperProject).join(
                        PaperProject, PaperProject.id == VisualAsset.project_id
                    )
                )
            ).all()
            for visual, project in visuals:
                renditions = dict(visual.renditions_json or {})
                changed_payload = False
                for item in renditions.values():
                    if not isinstance(item, dict):
                        continue
                    changed, key = _private_key(item.get("object_key"), project)
                    if changed:
                        copied += await _copy_verified(
                            store, item["object_key"], key, dry_run=dry_run
                        )
                        item["object_key"] = key
                        changed_payload = True
                if changed_payload and not dry_run:
                    visual.renditions_json = renditions

            documents = list((await session.scalars(select(DocumentFile))).all())
            for document in documents:
                old_key = document.object_key
                if old_key.startswith("works/"):
                    key = f"shared/oa/{old_key}"
                    copied += await _copy_verified(store, old_key, key, dry_run=dry_run)
                    if not dry_run:
                        document.object_key = key
                else:
                    skipped += 1
            if not dry_run:
                await session.commit()
        verb = "would copy" if dry_run else "copied and switched"
        print(f"object migration: {verb} {copied}; already migrated/empty: {skipped}")
        print("legacy objects were retained; remove them only after the seven-day safety window")
    finally:
        await engine.dispose()


def _private_key(old_key: str | None, project: PaperProject) -> tuple[bool, str | None]:
    if not old_key or old_key.startswith("users/"):
        return False, old_key
    legacy_prefix = f"projects/{project.id}/"
    if not old_key.startswith(legacy_prefix):
        return False, old_key
    return True, f"users/{project.owner_id}/{old_key}"


async def _copy_verified(store: Any, old_key: str, new_key: str, *, dry_run: bool) -> int:
    if dry_run:
        store.get(old_key)  # A dry run still verifies that every source exists.
        return 1
    data = store.get(old_key)
    digest = hashlib.sha256(data).digest()
    store.put(new_key, data)
    if hashlib.sha256(store.get(new_key)).digest() != digest:
        raise RuntimeError(f"object verification failed: {new_key}")
    return 1


async def _project_object_keys(session: Any, project_id: uuid.UUID) -> list[str]:
    """一个项目独占的对象键。

    必须逐条从库里枚举而不是按前缀删：`ObjectStore` 协议只有单键 `delete(key)`，
    没有 list/前缀删除。反过来说这也更安全——`shared/oa/works/...` 下的 OA 全文
    是**跨项目共享**的，按前缀扫会把别的项目还在用的全文一起删掉。
    """
    keys: list[str] = []
    assets = await session.scalars(select(UserAsset).where(UserAsset.project_id == project_id))
    keys.extend(asset.object_key for asset in assets)

    exports = await session.scalars(
        select(ExportArtifact).where(ExportArtifact.project_id == project_id)
    )
    for artifact in exports:
        keys.extend([artifact.object_key, artifact.compile_log_key])

    visuals = await session.scalars(select(VisualAsset).where(VisualAsset.project_id == project_id))
    for visual in visuals:
        for item in (visual.renditions_json or {}).values():
            if isinstance(item, dict):
                keys.append(item.get("object_key"))

    documents = await session.scalars(
        select(DocumentFile).where(DocumentFile.project_id == project_id)
    )
    keys.extend(document.object_key for document in documents)
    uploads = await session.scalars(
        select(LiteraturePdfUpload).where(LiteraturePdfUpload.project_id == project_id)
    )
    keys.extend(upload.object_key for upload in uploads)

    # 去重并保序；`shared/` 一律不碰。
    return list(dict.fromkeys(key for key in keys if key and not str(key).startswith("shared/")))


async def _purge_projects(*, days: int, dry_run: bool) -> None:
    """真删保留期已过的软删除项目，并回收它们独占的对象。

    顺序是**先对象后行**：反过来一旦行删成功、对象删失败，object_key 就再也查不出来，
    存储里会留下永远没人认领的垃圾。
    """
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    store = make_object_store(settings)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    purged = deleted_objects = missing_objects = 0
    try:
        async with factory() as session:
            projects = await list_purgeable_projects(session, before=cutoff)
            for project in projects:
                keys = await _project_object_keys(session, project.id)
                for key in keys:
                    if dry_run:
                        deleted_objects += 1
                        continue
                    try:
                        store.delete(key)
                        deleted_objects += 1
                    except Exception:  # noqa: BLE001 - 对象早已不在不该挡住回收
                        missing_objects += 1
                if not dry_run:
                    await purge_project(session, project)
                purged += 1
            if not dry_run:
                await session.commit()
    finally:
        await engine.dispose()
    prefix = "[dry-run] " if dry_run else ""
    print(
        f"{prefix}purged {purged} project(s) deleted before {cutoff.isoformat()}: "
        f"{deleted_objects} object(s) removed, {missing_objects} already gone"
    )


if __name__ == "__main__":
    main()
