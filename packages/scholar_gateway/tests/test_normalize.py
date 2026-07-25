from scholar_gateway import (
    normalize_arxiv_id,
    normalize_doi,
    normalize_pmid,
    normalized_title_hash,
    token_set_jaccard,
)


def test_normalize_doi_strips_prefixes_and_lowercases():
    assert normalize_doi("https://doi.org/10.1145/3292500.3330701") == "10.1145/3292500.3330701"
    assert normalize_doi("doi:10.1038/NPHYS1170") == "10.1038/nphys1170"
    assert normalize_doi("not a doi") is None


def test_normalize_arxiv_strips_version_and_scheme():
    assert normalize_arxiv_id("arXiv:2301.01234v3") == "2301.01234"
    assert normalize_arxiv_id("https://arxiv.org/pdf/2301.01234.pdf") == "2301.01234"


def test_normalize_pmid_trims_leading_zeros():
    assert normalize_pmid("PMID: 007123") == "7123"


def test_title_hash_is_stable_and_prefixed():
    h1 = normalized_title_hash("Attention Is All You Need")
    h2 = normalized_title_hash("attention   is all  you need")
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_token_set_jaccard_symmetry():
    a = "deep learning for graphs"
    b = "graphs for deep learning"
    assert token_set_jaccard(a, b) == 1.0
