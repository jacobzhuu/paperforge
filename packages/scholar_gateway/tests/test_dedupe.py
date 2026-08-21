from dataclasses import replace
from datetime import UTC, datetime

from scholar_gateway import ScholarlyWorkCandidate, dedupe_scholarly_candidates
from scholar_gateway.models import ScholarlyAuthorCandidate
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


def _authored(
    title: str,
    provider: str,
    *,
    author: str,
    year: int,
    doi: str | None = None,
    arxiv_id: str | None = None,
):
    base = _candidate(title, provider, doi=doi, year=year)
    return replace(
        base,
        arxiv_id=arxiv_id,
        authors=(ScholarlyAuthorCandidate(author_name=author, author_order=0),),
    )


def test_acronym_titled_preprint_and_published_version_collapse():
    """A renamed published version must not ship as a second reference.

    Real defect: "LoRec: Combating Poisons with Large Language Model…" and
    "LoRec: Large Language Model for Robust Sequential Recommendation…" were
    cited as `zhang2024lorec` and `zhang2024loreca` in one manuscript, which
    then compared the paper with itself.  Two guards had to give way: the
    fallback pass ignored leading segments shorter than 8 characters (so
    "LoRec" never keyed anything), and the arXiv DOI looked like a conflicting
    identifier next to the ACM DOI.
    """
    cands = [
        _authored(
            "LoRec: Large Language Model for Robust Sequential Recommendation "
            "against Poisoning",
            "arxiv",
            author="Kaike Zhang",
            year=2024,
            doi="10.48550/arXiv.2401.17723",
            arxiv_id="2401.17723",
        ),
        _authored(
            "LoRec: Combating Poisons with Large Language Model for Robust "
            "Sequential Recommendation",
            "crossref",
            author="Kaike Zhang",
            year=2024,
            doi="10.1145/3626772.3657684",
        ),
    ]
    result = dedupe_scholarly_candidates(cands)
    assert len(result.canonical_candidates) == 1
    assert result.duplicate_count == 1
    assert not result.conflicts


def test_arxiv_doi_does_not_conflict_with_a_publisher_doi():
    cands = [
        _authored(
            "Sim4Rec: Data-Free Model Extraction",
            "arxiv",
            author="Yu Wang",
            year=2025,
            doi="10.48550/arXiv.2501.00001",
        ),
        _authored(
            "Sim4Rec: Data-Free Model Extraction",
            "crossref",
            author="Yu Wang",
            year=2025,
            doi="10.1145/9999999",
        ),
    ]
    result = dedupe_scholarly_candidates(cands)
    assert len(result.canonical_candidates) == 1
    assert not result.conflicts


def test_same_acronym_different_year_is_left_alone():
    """DARTS (2025) and DV-FSR (2024) share an author and a framework name.

    They may or may not be the same work.  Dedupe must not guess — the corpus
    keeps both and the duplicate-reference quality check raises it for review.
    """
    cands = [
        _authored(
            "DARTS: A Dual-View Attack Framework for Federated Sequential Recommendation",
            "arxiv",
            author="Qitao Qin",
            year=2025,
            arxiv_id="2507.01383",
        ),
        _authored(
            "DV-FSR: A Dual-View Target Attack Framework for Federated Sequential Recommendation",
            "arxiv",
            author="Qitao Qin",
            year=2024,
            arxiv_id="2409.07500",
        ),
    ]
    result = dedupe_scholarly_candidates(cands)
    assert len(result.canonical_candidates) == 2


def test_short_prefix_alone_does_not_merge_unrelated_papers():
    cands = [
        _authored(
            "GAN: Generative Adversarial Networks", "openalex", author="Ann Smith", year=2020
        ),
        _authored("GAN: Graph Attention Networks", "crossref", author="Bob Jones", year=2020),
    ]
    result = dedupe_scholarly_candidates(cands)
    assert len(result.canonical_candidates) == 2
