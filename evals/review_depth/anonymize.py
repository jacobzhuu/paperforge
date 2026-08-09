"""Create a reproducible, double-blind G3 packet from generated Markdown manuscripts."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from pathlib import Path


def anonymize(
    *,
    manifest: list[dict],
    source_root: Path,
    output_root: Path,
    seed: int,
) -> list[dict[str, str]]:
    output_root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    rows = list(manifest)
    rng.shuffle(rows)
    key: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        topic_id = str(row["topic_id"])
        condition = str(row["condition"])
        source = source_root / str(row["path"])
        digest = hashlib.sha1(f"{seed}:{topic_id}:{condition}".encode()).hexdigest()[:6]
        blind_id = f"R{index:03d}-{digest}"
        content = source.read_text(encoding="utf-8")
        content = re.sub(
            r"(?im)^(?:condition|system|pipeline|generator)\s*:\s*.*$",
            "",
            content,
        )
        (output_root / f"{blind_id}.md").write_text(content.strip() + "\n", encoding="utf-8")
        key.append(
            {
                "blind_doc_id": blind_id,
                "topic_id": topic_id,
                "condition": condition,
            }
        )
    return key


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--key-output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260730)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    key = anonymize(
        manifest=manifest,
        source_root=args.source_root,
        output_root=args.output_root,
        seed=args.seed,
    )
    args.key_output.write_text(
        json.dumps(key, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
