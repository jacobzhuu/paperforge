from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import hashlib
import uuid
from pathlib import Path
from typing import Any

from db import create_user, get_user_by_email
from db.models.auth import AppUser
from db.models.library import DocumentFile
from db.models.paper import ExportArtifact, PaperProject, UserAsset, VisualAsset
from db.session import make_engine, make_session_factory
from sqlalchemy import select, update
from storage import make_object_store

from paperforge_api.auth_service import hash_password
from paperforge_api.config import get_settings

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
    migrate = commands.add_parser("migrate-objects", help="copy legacy keys into tenant namespaces")
    migrate.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "bootstrap-admin":
        password = getpass.getpass("New administrator password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise SystemExit("passwords do not match")
        password_hash = hash_password(password)
        asyncio.run(_bootstrap(args, password_hash))
    else:
        asyncio.run(_migrate_objects(dry_run=args.dry_run))


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


if __name__ == "__main__":
    main()
