from datetime import UTC, datetime

from scholar_gateway import ScholarlyWorkCandidate, dedupe_scholarly_candidates
from scholar_gateway.normalize import normalize_title_for_dedupe, normalized_title_hash


def _candidate(title: str, provider: str, *, doi: str | None = None, year: int | None = None):
    return ScholarlyWorkCandidate(
        title=title,
        normalized_title=normalize_title_for_dedupe(title) or title,
        normalized_title_hash=normalized_title_hash(title) or "",
        provider_name=provider,
        provider_record_id=f"{provider}-{title[:6]}",
        provider_record_url=None,
        query_text="q",
        retrieved_at=datetime.now(tz=UTC),
        doi=doi,
        publication_year=year,
    )


def test_same_doi_across_providers_collapses_to_one():
    cands = [
        _candidate("Attention Is All You Need", "openalex", doi="10.5555/3295222"),
        _candidate("Attention is all you need", "crossref", doi="10.5555/3295222"),
    ]
    result = dedupe_scholarly_candidates(cands)
    assert len(result.canonical_candidates) == 1
    assert result.duplicate_count == 1


def test_distinct_works_are_not_merged():
    cands = [
        _candidate("A Study of Graphs", "openalex", doi="10.1/aaa", year=2021),
        _candidate("A Study of Trees", "crossref", doi="10.1/bbb", year=2022),
    ]
    result = dedupe_scholarly_candidates(cands)
    assert len(result.canonical_candidates) == 2
    assert result.duplicate_count == 0
