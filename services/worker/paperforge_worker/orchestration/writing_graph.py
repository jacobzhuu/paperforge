"""Writing contracts and a conservative DAG, independent of queues and providers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

WRITING_VERSION = "writing-v2"
FRAME_KEYS = frozenset({"abstract", "introduction", "conclusion"})


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def is_frame(section: dict[str, Any]) -> bool:
    return section.get("kind") == "frame" or section.get("key") in FRAME_KEYS


def section_graph(sections: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Absent contracts retain order. Only explicitly independent sections fan out.

    `depends_on` is a list of section keys. `independent: true` means no body
    dependency; it is not inferred from headings or question IDs. Frame barriers
    are mandatory even when a supplied contract omits them.
    """
    keys = [str(item.get("key") or "") for item in sections]
    if not all(keys) or len(keys) != len(set(keys)):
        raise ValueError("writing graph requires unique nonempty section keys")
    body = [str(s["key"]) for s in sections if not is_frame(s)]
    graph: dict[str, list[str]] = {}
    previous: str | None = None
    for item, key in zip(sections, keys, strict=True):
        if is_frame(item):
            graph[key] = list(body)
            continue
        declared = item.get("depends_on")
        if declared is not None:
            if not isinstance(declared, list) or any(not isinstance(v, str) for v in declared):
                raise ValueError(f"invalid dependencies for {key}")
            dependencies = list(dict.fromkeys(declared))
        else:
            dependencies = (
                [] if item.get("independent") is True else ([previous] if previous else [])
            )
        if key in dependencies or any(dep not in body for dep in dependencies):
            raise ValueError(f"invalid body dependency for {key}")
        graph[key] = dependencies
        previous = key
    layers(graph)  # Cycle validation before any paid call.
    return graph


def layers(graph: dict[str, list[str]]) -> list[list[str]]:
    done: set[str] = set()
    result: list[list[str]] = []
    while len(done) < len(graph):
        ready = [key for key, deps in graph.items() if key not in done and set(deps) <= done]
        if not ready:
            raise ValueError("cyclic or missing writing dependency")
        result.append(ready)
        done.update(ready)
    return result


def descendants(graph: dict[str, list[str]], changed: set[str]) -> set[str]:
    affected = set(changed)
    for wave in layers(graph):
        affected.update(key for key in wave if set(graph[key]) & affected)
    return affected


def findings_snapshot(sections: list[dict], summaries: dict[str, str]) -> str:
    # Bounded by section count, not a trailing slice that drops early findings.
    body = [s for s in sections if not is_frame(s)]
    budget = max(120, 12000 // max(1, len(body)))
    return "\n".join(
        f"[{s['key']}] {s.get('title', '')}: "
        f"{summaries.get(str(s['key']), '(not written)')[:budget]}"
        for s in body
    )


def merge_terms(base: dict[str, str], proposals: list[tuple[str, dict]]) -> tuple[dict, list[dict]]:
    """Outline order wins, never completion order; preserve conflicts for Integration."""
    glossary = dict(base)
    conflicts = []
    for key, terms in proposals:
        for term, value in sorted(terms.items()):
            if term in glossary and glossary[term] != value:
                conflicts.append(
                    {"section": key, "term": term, "canonical": glossary[term], "proposed": value}
                )
            else:
                glossary[term] = value
    return glossary, conflicts
