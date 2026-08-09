"""Validate that human gold fixtures are complete before a release evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate_g1(rows: list[dict], *, allow_incomplete: bool = False) -> list[str]:
    errors: list[str] = []
    if len(rows) != 20:
        errors.append(f"G1 must contain exactly 20 papers; found {len(rows)}")
    for row in rows:
        paper_id = row.get("paper_id", "unknown")
        verified = row.get("annotation_status") == "expert_verified"
        if not verified:
            if not allow_incomplete:
                errors.append(f"{paper_id}: annotation is not expert_verified")
            continue
        if not row.get("oa_url") or not row.get("source_sha256"):
            errors.append(f"{paper_id}: verified annotation lacks OA URL/source hash")
        roles = {
            str(section.get("role"))
            for section in row.get("sections") or []
            if isinstance(section, dict)
        }
        if not {"methods", "results"}.issubset(roles):
            errors.append(f"{paper_id}: verified annotation lacks methods/results spans")
        for obj in row.get("objects") or []:
            if obj.get("kind") not in {"table", "equation", "figure_caption"}:
                errors.append(f"{paper_id}: unsupported object kind {obj.get('kind')}")
            if not obj.get("locator"):
                errors.append(f"{paper_id}: annotated object lacks locator")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--g1",
        type=Path,
        default=Path(__file__).parent / "fixtures/g1_annotation_slots.json",
    )
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    rows = json.loads(args.g1.read_text(encoding="utf-8"))
    errors = validate_g1(rows, allow_incomplete=args.allow_incomplete)
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"validated {len(rows)} G1 annotations")  # noqa: T201


if __name__ == "__main__":
    main()

