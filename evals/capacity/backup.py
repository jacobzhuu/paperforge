"""Restore ONLY the disposable capacity database and bucket; never production."""

import hashlib
import json
import subprocess
import time
from pathlib import Path

from paperforge_api.storage_migration import export_storage, restore_storage
from storage import MinioObjectStore


def command(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sql(database, query):
    return command(
        "docker",
        "exec",
        "pf-capacity-test-pg",
        "psql",
        "-U",
        "paperforge",
        "-d",
        database,
        "-Atc",
        query,
        capture_output=True,
        text=True,
    ).stdout.strip()


def store(bucket):
    return MinioObjectStore(
        endpoint="127.0.0.1:29000",
        access_key="capacity",
        secret_key="capacity-test-only",
        bucket=bucket,
    )


def main():
    assert (
        sql(
            "paperforge_capacity",
            "SELECT count(*) FROM generation_job WHERE status IN ('queued','running')",
        )
        == "0"
    ), "drain fixture jobs first"
    stamp = str(time.time_ns())
    target = "paperforge_capacity_restore_" + stamp
    directory = Path(".paperforge/capacity/backup") / stamp
    directory.mkdir(parents=True)
    original = store("capacity")
    payload = b"PaperForge isolated recovery fixture\n" * 4096
    key = "recovery/" + stamp + ".bin"
    original.put(key, payload)
    start = time.monotonic()
    with (directory / "database.dump").open("wb") as output:
        command(
            "docker",
            "exec",
            "pf-capacity-test-pg",
            "pg_dump",
            "-U",
            "paperforge",
            "-d",
            "paperforge_capacity",
            "-Fc",
            stdout=output,
        )
    entries = export_storage(
        store=original,
        archive_path=directory / "objects.tar.gz",
        manifest_path=directory / "manifest.json",
    )
    backup_seconds = time.monotonic() - start
    command("docker", "exec", "pf-capacity-test-pg", "createdb", "-U", "paperforge", target)
    start = time.monotonic()
    with (directory / "database.dump").open("rb") as source:
        command(
            "docker",
            "exec",
            "-i",
            "pf-capacity-test-pg",
            "pg_restore",
            "-U",
            "paperforge",
            "-d",
            target,
            "--exit-on-error",
            stdin=source,
        )
    destination = store("capacity-restore-" + stamp)
    restored = restore_storage(
        archive_path=directory / "objects.tar.gz",
        manifest_path=directory / "manifest.json",
        destination=destination,
    )
    restore_storage(
        archive_path=directory / "objects.tar.gz",
        manifest_path=directory / "manifest.json",
        destination=destination,
        verify_only=True,
    )
    assert destination.get(key) == payload
    counts = {}
    for table in (
        "paper_project",
        "generation_job",
        "job_event",
        "job_dispatch",
        "alembic_version",
    ):
        before = sql("paperforge_capacity", f"SELECT count(*) FROM {table}")
        after = sql(target, f"SELECT count(*) FROM {table}")
        assert before == after, table
        counts[table] = int(after)
    result = {
        "kind": "isolated_quiescent_restore_not_production_RTO",
        "backup_seconds": backup_seconds,
        "restore_and_verify_seconds": time.monotonic() - start,
        "object_count": len(entries),
        "objects": restored,
        "table_row_counts": counts,
        "fixture_sha256": hashlib.sha256(payload).hexdigest(),
        "fixture_bytes": len(payload),
        "migration": sql(target, "SELECT version_num FROM alembic_version"),
        "passed": True,
    }
    Path("evals/capacity/results/backup-restore.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
