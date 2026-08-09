#!/usr/bin/env python3
"""Fail CI when domain-specific literals reappear in ontology-driven modules (R16)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ROOT / "services/worker/paperforge_worker/pipelines/evidence.py",
    ROOT / "services/worker/paperforge_worker/pipelines/qdecomp.py",
]
# These must live in task_definition rows, not Python source.
FORBIDDEN = re.compile(
    r"\b(?:MIBiG|antiSMASH-DB|IMG-ABC|DeepBGC|GECCO|clusterFinder|BiG-SLiCE|"
    r"MovieLens|Amazon Beauty|LastFM)\b"
)


def main() -> int:
    failures: list[str] = []
    for path in TARGETS:
        text = path.read_text(encoding="utf-8")
        for match in FORBIDDEN.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            failures.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)}")
    if failures:
        print("Domain literals found outside task_definition (R16):")
        for item in failures:
            print(f"  {item}")
        return 1
    print("ontology_literal_lint: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
