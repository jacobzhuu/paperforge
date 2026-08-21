# 迁移自 DeepSearch literature_review/dedupe.py。
# 改动：import 路径改为 scholar_gateway；候选模型解耦自 discovery。
# dedupe_conflict 现为信息性标记（保留双记录并打标），不含人工仲裁流（方案 §3.1）。
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scholar_gateway.models import ScholarlyWorkCandidate
from scholar_gateway.normalize import (
    normalize_arxiv_id,
    normalize_corpus_id,
    normalize_doi,
    normalize_openalex_id,
    normalize_pmcid,
    normalize_pmid,
    normalize_semantic_scholar_id,
    normalize_title_for_dedupe,
    normalized_title_hash,
)

EXACT_IDENTIFIER_TYPES = (
    "doi",
    "pmid",
    "pmcid",
    "arxiv",
    "openalex",
    "semantic_scholar",
    "corpus_id",
)
_IDENTIFIER_PRIORITY = {
    "doi": 1,
    "pmid": 2,
    "pmcid": 3,
    "arxiv": 4,
    "openalex": 5,
    "semantic_scholar": 6,
    "corpus_id": 7,
    "title_hash": 8,
    "normalized_title_hash": 8,
    "title_year_first_author": 9,
}
_CONFIDENCE_BY_REASON = {
    "doi_exact": 1.0,
    "pmid_exact": 0.99,
    "pmcid_exact": 0.99,
    "arxiv_exact": 0.99,
    "openalex_exact": 0.98,
    "semantic_scholar_exact": 0.98,
    "corpus_id_exact": 0.98,
    "normalized_title_hash": 0.93,
    "title_year_first_author": 0.88,
}


@dataclass(frozen=True)
class DedupeCandidateFingerprint:
    candidate_index: int
    normalized_title: str | None
    normalized_title_hash: str | None
    fallback_title: str | None
    first_author: str | None
    publication_year: int | None
    identifiers: dict[str, tuple[str, ...]]

    @property
    def exact_identifier_count(self) -> int:
        return sum(len(values) for values in self.identifiers.values())


@dataclass(frozen=True)
class DedupeClusterMember:
    candidate_index: int
    provider_name: str
    provider_record_id: str | None
    title: str
    match_features: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DedupeCluster:
    cluster_key: str
    canonical_candidate_index: int
    reason: str
    confidence: float
    members: tuple[DedupeClusterMember, ...]


@dataclass(frozen=True)
class DedupeConflict:
    conflict_type: str
    candidate_indexes: tuple[int, ...]
    identifier_types: tuple[str, ...]
    match_basis: str
    blocked_merge: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DedupeResult:
    clusters: tuple[DedupeCluster, ...]
    canonical_candidates: tuple[ScholarlyWorkCandidate, ...]
    duplicate_count: int
    conflicts: tuple[DedupeConflict, ...] = ()


def dedupe_scholarly_candidates(
    candidates: tuple[ScholarlyWorkCandidate, ...] | list[ScholarlyWorkCandidate],
) -> DedupeResult:
    """Return deterministic duplicate clusters without mutating persistence state."""
    candidate_tuple = tuple(candidates)
    fingerprints = tuple(
        _fingerprint_candidate(candidate, index) for index, candidate in enumerate(candidate_tuple)
    )
    union = _UnionFind(len(candidate_tuple))
    match_features_by_index: dict[int, dict[str, Any]] = {
        index: {"match_keys": []} for index in range(len(candidate_tuple))
    }
    conflicts: list[DedupeConflict] = []
    blocked_conflict_groups: set[tuple[int, ...]] = set()

    for id_type in EXACT_IDENTIFIER_TYPES:
        for id_value, indexes in _identifier_groups(fingerprints, id_type).items():
            if len(indexes) < 2:
                continue
            reason = f"{id_type}_exact"
            _union_indexes(union, indexes)
            _append_match_features(
                match_features_by_index,
                indexes,
                {
                    "match_type": reason,
                    "identifier_type": id_type,
                    "identifier_value": id_value,
                    "confidence": _CONFIDENCE_BY_REASON[reason],
                },
            )

    for title_hash, indexes in _title_hash_groups(fingerprints).items():
        if len(indexes) < 2:
            continue
        if not _already_clustered(union, indexes):
            conflict = _identifier_conflict(
                fingerprints,
                indexes,
                match_basis="normalized_title_hash",
                blocked_merge=True,
            )
            if conflict is not None:
                conflicts.append(conflict)
                blocked_conflict_groups.add(tuple(sorted(indexes)))
                continue
            _union_indexes(union, indexes)
        _append_match_features(
            match_features_by_index,
            indexes,
            {
                "match_type": "normalized_title_hash",
                "normalized_title_hash": title_hash,
                "confidence": _CONFIDENCE_BY_REASON["normalized_title_hash"],
            },
        )

    for fallback_key, indexes in _title_year_first_author_groups(fingerprints).items():
        if len(indexes) < 2:
            continue
        sorted_indexes = tuple(sorted(indexes))
        if sorted_indexes in blocked_conflict_groups:
            continue
        if not _already_clustered(union, indexes):
            conflict = _identifier_conflict(
                fingerprints,
                indexes,
                match_basis="title_year_first_author",
                blocked_merge=True,
            )
            if conflict is not None:
                conflicts.append(conflict)
                blocked_conflict_groups.add(sorted_indexes)
                continue
            _union_indexes(union, indexes)
        title, year, first_author = fallback_key
        _append_match_features(
            match_features_by_index,
            indexes,
            {
                "match_type": "title_year_first_author",
                "normalized_title": title,
                "publication_year": year,
                "first_author": first_author,
                "confidence": _CONFIDENCE_BY_REASON["title_year_first_author"],
            },
        )

    grouped_indexes: dict[int, list[int]] = {}
    for index in range(len(candidate_tuple)):
        grouped_indexes.setdefault(union.find(index), []).append(index)

    clusters: list[DedupeCluster] = []
    canonical_indexes: list[int] = []
    for indexes in grouped_indexes.values():
        indexes.sort()
        canonical_index = _canonical_index(candidate_tuple, fingerprints, indexes)
        canonical_indexes.append(canonical_index)
        if len(indexes) < 2:
            continue
        cluster_features = _cluster_match_features(match_features_by_index, indexes)
        reason = _best_reason(cluster_features)
        members = tuple(
            DedupeClusterMember(
                candidate_index=index,
                provider_name=candidate_tuple[index].provider_name,
                provider_record_id=candidate_tuple[index].provider_record_id,
                title=candidate_tuple[index].title,
                match_features={
                    "canonical": index == canonical_index,
                    "normalized_title": fingerprints[index].normalized_title,
                    "normalized_title_hash": fingerprints[index].normalized_title_hash,
                    "first_author": fingerprints[index].first_author,
                    "publication_year": fingerprints[index].publication_year,
                    "identifiers": fingerprints[index].identifiers,
                    "match_keys": match_features_by_index[index]["match_keys"],
                },
            )
            for index in indexes
        )
        conflicts.extend(
            conflict
            for conflict in (
                _identifier_conflict(
                    fingerprints,
                    indexes,
                    match_basis=reason,
                    blocked_merge=False,
                ),
            )
            if conflict is not None
        )
        clusters.append(
            DedupeCluster(
                cluster_key=_cluster_key(candidate_tuple, fingerprints, canonical_index),
                canonical_candidate_index=canonical_index,
                reason=reason,
                confidence=_CONFIDENCE_BY_REASON[reason],
                members=members,
            )
        )

    canonical_indexes.sort(
        key=lambda index: _candidate_sort_key(candidate_tuple[index], fingerprints[index])
    )
    clusters.sort(key=lambda cluster: cluster.cluster_key)
    unique_conflicts = _dedupe_conflicts(conflicts)
    return DedupeResult(
        clusters=tuple(clusters),
        canonical_candidates=tuple(candidate_tuple[index] for index in canonical_indexes),
        duplicate_count=len(candidate_tuple) - len(canonical_indexes),
        conflicts=unique_conflicts,
    )


def _fingerprint_candidate(
    candidate: ScholarlyWorkCandidate, candidate_index: int
) -> DedupeCandidateFingerprint:
    normalized_title = normalize_title_for_dedupe(candidate.title) or normalize_title_for_dedupe(
        candidate.normalized_title
    )
    title_hash = normalized_title_hash(candidate.title) or (
        candidate.normalized_title_hash
        if candidate.normalized_title_hash.startswith("sha256:")
        else None
    )
    identifiers = _normalized_identifiers(candidate)
    first_author = _normalized_first_author(candidate)
    return DedupeCandidateFingerprint(
        candidate_index=candidate_index,
        normalized_title=normalized_title,
        normalized_title_hash=title_hash,
        fallback_title=_fallback_title_for_matching(candidate.title) or normalized_title,
        first_author=first_author,
        publication_year=_valid_publication_year(candidate.publication_year),
        identifiers=identifiers,
    )


def _normalized_identifiers(candidate: ScholarlyWorkCandidate) -> dict[str, tuple[str, ...]]:
    values: dict[str, set[str]] = {id_type: set() for id_type in EXACT_IDENTIFIER_TYPES}
    _add_identifier(values, "doi", candidate.doi)
    _add_identifier(values, "pmid", candidate.pmid)
    _add_identifier(values, "pmcid", candidate.pmcid)
    _add_identifier(values, "arxiv", candidate.arxiv_id)
    _add_identifier(values, "openalex", candidate.openalex_id)
    _add_identifier(values, "semantic_scholar", candidate.semantic_scholar_id)
    _add_identifier(values, "corpus_id", candidate.corpus_id)
    for identifier in candidate.identifiers:
        id_type = identifier.id_type.strip().lower().replace("-", "_")
        if id_type == "arxiv_id":
            id_type = "arxiv"
        if id_type in {"semantic_scholar_id", "paper_id"}:
            id_type = "semantic_scholar"
        _add_identifier(values, id_type, identifier.id_value)
    return {id_type: tuple(sorted(id_values)) for id_type, id_values in values.items() if id_values}


#: arXiv mints a DOI per *submission*, so ``10.48550/arxiv.2401.17723`` and a
#: publisher's DOI for the same paper are not rival claims about one work — they
#: identify two manifestations of it.  Treating the arXiv one as an ordinary DOI
#: made every preprint/published pair look like an identifier conflict, which
#: blocks the merge and ships the paper twice in one bibliography.
_ARXIV_DOI_PREFIX = "10.48550/arxiv."


def _add_identifier(values: dict[str, set[str]], id_type: str, raw_value: object) -> None:
    normalized: str | None
    if id_type == "doi":
        normalized = normalize_doi(str(raw_value)) if raw_value is not None else None
        if normalized and normalized.startswith(_ARXIV_DOI_PREFIX):
            # Re-file it as the arXiv id it actually is.
            _add_identifier(values, "arxiv", normalized[len(_ARXIV_DOI_PREFIX) :])
            return
    elif id_type == "pmid":
        normalized = normalize_pmid(raw_value)  # type: ignore[arg-type]
    elif id_type == "pmcid":
        normalized = normalize_pmcid(raw_value)  # type: ignore[arg-type]
    elif id_type == "arxiv":
        normalized = normalize_arxiv_id(str(raw_value)) if raw_value is not None else None
    elif id_type == "openalex":
        normalized = normalize_openalex_id(str(raw_value)) if raw_value is not None else None
    elif id_type == "semantic_scholar":
        normalized = (
            normalize_semantic_scholar_id(str(raw_value)) if raw_value is not None else None
        )
    elif id_type == "corpus_id":
        normalized = normalize_corpus_id(raw_value)  # type: ignore[arg-type]
    else:
        return
    if normalized:
        values.setdefault(id_type, set()).add(normalized)


def _normalized_first_author(candidate: ScholarlyWorkCandidate) -> str | None:
    if not candidate.authors:
        return None
    author = min(candidate.authors, key=lambda item: item.author_order)
    return normalize_title_for_dedupe(author.author_name)


#: A short leading segment is only usable because the fallback pass keys on
#: ``(title, year, first_author)`` — the prefix never matches alone.  The old
#: floor of 8 characters silently disabled this pass for exactly the titles it
#: exists to catch: "LoRec:", "DARTS:", "PGAN:" are all 4-6 characters, so a
#: preprint and its renamed published version shipped as two references.
_MIN_FALLBACK_TITLE = 4


def _fallback_title_for_matching(title: str) -> str | None:
    first_segment = re_split_title_segment(title)
    if first_segment is None:
        return None
    normalized = normalize_title_for_dedupe(first_segment)
    if normalized is None or len(normalized) < _MIN_FALLBACK_TITLE:
        return None
    return normalized


def re_split_title_segment(title: str) -> str | None:
    separators = (" -- ", " - ", ":")
    for separator in separators:
        if separator in title:
            segment = title.split(separator, 1)[0].strip()
            return segment or None
    return None


def _valid_publication_year(value: int | None) -> int | None:
    if value is None or value < 1000 or value > 9999:
        return None
    return value


def _identifier_groups(
    fingerprints: tuple[DedupeCandidateFingerprint, ...], id_type: str
) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = {}
    for fingerprint in fingerprints:
        for value in fingerprint.identifiers.get(id_type, ()):
            groups.setdefault(value, []).append(fingerprint.candidate_index)
    return groups


def _title_hash_groups(
    fingerprints: tuple[DedupeCandidateFingerprint, ...],
) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = {}
    for fingerprint in fingerprints:
        if fingerprint.normalized_title_hash:
            groups.setdefault(fingerprint.normalized_title_hash, []).append(
                fingerprint.candidate_index
            )
    return groups


def _title_year_first_author_groups(
    fingerprints: tuple[DedupeCandidateFingerprint, ...],
) -> dict[tuple[str, int, str], list[int]]:
    groups: dict[tuple[str, int, str], list[int]] = {}
    for fingerprint in fingerprints:
        if (
            fingerprint.fallback_title is None
            or fingerprint.publication_year is None
            or fingerprint.first_author is None
        ):
            continue
        key = (
            fingerprint.fallback_title,
            fingerprint.publication_year,
            fingerprint.first_author,
        )
        groups.setdefault(key, []).append(fingerprint.candidate_index)
    return groups


def _identifier_conflict(
    fingerprints: tuple[DedupeCandidateFingerprint, ...],
    indexes: list[int],
    *,
    match_basis: str,
    blocked_merge: bool,
) -> DedupeConflict | None:
    conflict_values: dict[str, dict[int, tuple[str, ...]]] = {}
    for id_type in EXACT_IDENTIFIER_TYPES:
        values_by_candidate = {
            index: fingerprints[index].identifiers[id_type]
            for index in indexes
            if id_type in fingerprints[index].identifiers
        }
        if len({values for values in values_by_candidate.values()}) > 1:
            conflict_values[id_type] = values_by_candidate
    if not conflict_values:
        return None
    return DedupeConflict(
        conflict_type="conflicting_identifier_values",
        candidate_indexes=tuple(sorted(indexes)),
        identifier_types=tuple(sorted(conflict_values)),
        match_basis=match_basis,
        blocked_merge=blocked_merge,
        details={
            id_type: {str(index): values for index, values in sorted(values_by_candidate.items())}
            for id_type, values_by_candidate in sorted(conflict_values.items())
        },
    )


def _union_indexes(union: _UnionFind, indexes: list[int]) -> None:
    first = indexes[0]
    for index in indexes[1:]:
        union.union(first, index)


def _already_clustered(union: _UnionFind, indexes: list[int]) -> bool:
    roots = {union.find(index) for index in indexes}
    return len(roots) == 1


def _append_match_features(
    features_by_index: dict[int, dict[str, Any]],
    indexes: list[int],
    feature: dict[str, Any],
) -> None:
    for index in indexes:
        features_by_index[index]["match_keys"].append(feature)


def _cluster_match_features(
    features_by_index: dict[int, dict[str, Any]], indexes: list[int]
) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index in indexes:
        for feature in features_by_index[index]["match_keys"]:
            key = (feature["match_type"], str(feature))
            if key in seen:
                continue
            seen.add(key)
            features.append(feature)
    return features


def _best_reason(features: list[dict[str, Any]]) -> str:
    reasons = {str(feature["match_type"]) for feature in features}
    return min(
        reasons,
        key=lambda reason: (
            _IDENTIFIER_PRIORITY.get(reason.removesuffix("_exact"), 100),
            reason,
        ),
    )


def _canonical_index(
    candidates: tuple[ScholarlyWorkCandidate, ...],
    fingerprints: tuple[DedupeCandidateFingerprint, ...],
    indexes: list[int],
) -> int:
    return min(
        indexes,
        key=lambda index: _candidate_sort_key(candidates[index], fingerprints[index]),
    )


def _candidate_sort_key(
    candidate: ScholarlyWorkCandidate,
    fingerprint: DedupeCandidateFingerprint,
) -> tuple[Any, ...]:
    completeness_score = (
        fingerprint.exact_identifier_count * 20
        + (10 if candidate.abstract else 0)
        + (5 if fingerprint.publication_year is not None else 0)
        + min(len(candidate.authors), 5)
        + min(len(candidate.links), 5)
    )
    return (
        -completeness_score,
        candidate.is_retracted,
        candidate.provider_name,
        candidate.provider_record_id or "",
        fingerprint.normalized_title or "",
        fingerprint.candidate_index,
    )


def _cluster_key(
    candidates: tuple[ScholarlyWorkCandidate, ...],
    fingerprints: tuple[DedupeCandidateFingerprint, ...],
    canonical_index: int,
) -> str:
    fingerprint = fingerprints[canonical_index]
    for id_type in EXACT_IDENTIFIER_TYPES:
        values = fingerprint.identifiers.get(id_type)
        if values:
            return f"{id_type}:{values[0]}"
    if fingerprint.normalized_title_hash:
        return f"title_hash:{fingerprint.normalized_title_hash}"
    provider_record_id = candidates[canonical_index].provider_record_id or str(canonical_index)
    return f"candidate:{candidates[canonical_index].provider_name}:{provider_record_id}"


def _dedupe_conflicts(conflicts: list[DedupeConflict]) -> tuple[DedupeConflict, ...]:
    unique: dict[tuple[Any, ...], DedupeConflict] = {}
    for conflict in conflicts:
        key = (
            conflict.conflict_type,
            conflict.candidate_indexes,
            conflict.identifier_types,
            conflict.match_basis,
            conflict.blocked_merge,
        )
        unique[key] = conflict
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (item[1], item[2], item[3], item[4], item[0]),
        )
    )


class _UnionFind:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, index: int) -> int:
        parent = self._parent[index]
        if parent != index:
            self._parent[index] = self.find(parent)
        return self._parent[index]

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            self._parent[right_root] = left_root
        else:
            self._parent[left_root] = right_root
