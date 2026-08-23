"""精确字符区间要从全文解析一路走到证据记录，并且真的指得回原文。

解析器早就算出了 ``char_start``/``char_end``（生产库 27,451 个 chunk 100% 有值，
且全部满足 ``char_end - char_start == len(text)``），但 ``evidence_unit`` 里
**11,731 行没有一行有值**。断在四处：

1. ``fulltext._located_chunk_text`` 手里拿着 ``char_start``，却从没把它写进标记；
2. ``evidence._parse_locator`` 不认 ``CHAR``；
3. ``_located_fulltext_candidates`` 用 ``split`` + ``strip`` 切段，偏移两步都丢了；
4. ``EvidenceCandidate`` 没有这两个字段，落库时自然也就没传。

这里逐段钉住这条链，并验证区间切出来的子串就是证据原文。
"""

from __future__ import annotations

import re

from paperforge_worker.pipelines.evidence import (
    _chunk_spans,
    _evidence_candidates,
    _locate_excerpt,
    _located_fulltext_candidates,
    _parse_locator,
    _passage_span,
    _split_passages,
)
from paperforge_worker.pipelines.fulltext import _located_chunk_text


class _Chunk:
    """`_located_chunk_text` 只用到 `.metadata` 与 `.text`。"""

    def __init__(self, text: str, metadata: dict) -> None:
        self.text = text
        self.metadata = metadata


# --- 1. 标记侧：偏移必须被发出来 -----------------------------------------------


def test_the_marker_carries_the_character_span():
    chunk = _Chunk(
        "Accuracy improved from 61.2% to 78.9% on the held-out split.",
        {"char_start": 1200, "char_end": 1260, "page_number": 4, "section_title": "Results"},
    )
    rendered = _located_chunk_text(chunk, {})
    assert "CHAR=1200-1260" in rendered
    assert rendered.endswith(chunk.text)


def test_a_chunk_without_offsets_emits_no_char_marker():
    """没有可靠偏移时不发标记，下游据此留空而不是猜。"""
    chunk = _Chunk("Some prose.", {"page_number": 2})
    assert "CHAR=" not in _located_chunk_text(chunk, {})


def test_a_zero_offset_chunk_still_emits_its_span():
    """文档第一个 chunk 的 char_start 就是 0；旧代码的 `or 0` 分不出「没有」和 0。"""
    chunk = _Chunk("Opening paragraph.", {"char_start": 0, "char_end": 18})
    assert "CHAR=0-18" in _located_chunk_text(chunk, {})


# --- 2. 解析侧 ----------------------------------------------------------------


def test_the_locator_parser_reads_the_span():
    locator = _parse_locator("PAGE=4 | SECTION=Results | CHAR=1200-1260")
    assert locator["char_start"] == 1200
    assert locator["char_end"] == 1260
    # 既有 locator contract 不变。
    assert locator["page"] == 4
    assert locator["section"] == "Results"


def test_a_malformed_span_is_ignored_rather_than_guessed():
    for bad in ("CHAR=abc-def", "CHAR=1200", "CHAR=1260-1200", "CHAR=-", "CHAR=1200-"):
        locator = _parse_locator(bad)
        assert "char_start" not in locator, bad
        assert "char_end" not in locator, bad


# --- 3. 切段：偏移不能在 split/strip 里丢 ---------------------------------------


def test_splitting_passages_keeps_each_offset():
    block = "First passage.\n\n\nSecond passage here.\n\n  Third."
    passages = _split_passages(block)
    assert [text for _offset, text in passages] == [
        "First passage.",
        "Second passage here.",
        "Third.",
    ]
    for offset, text in passages:
        assert block[offset : offset + len(text)] == text, "偏移必须切得出原文"


def test_a_passage_span_is_the_chunk_offset_plus_the_passage_offset():
    locator = {"char_start": 1000, "char_end": 1100}
    assert _passage_span(locator, 20, 30) == (1020, 1050)


def test_a_span_that_would_run_past_the_chunk_is_clipped():
    locator = {"char_start": 1000, "char_end": 1050}
    assert _passage_span(locator, 40, 30) == (1040, 1050)


def test_no_marker_means_no_span():
    assert _passage_span({}, 10, 20) == (None, None)
    assert _passage_span({"char_start": 5}, 10, 20) == (None, None)


# --- 4. 端到端：解析产物 → 候选 -------------------------------------------------


DOC = (
    "Introduction text that sets up the study and is long enough to be a chunk. "
    "It continues for a while.\n\n"
    "Accuracy improved from 61.2% to 78.9% on the held-out split of the benchmark.\n\n"
    "Recall fell to 38.0% once the injection ratio dropped below 0.2 percent."
)


def _fulltext_from(document: str) -> tuple[str, list[_Chunk]]:
    """把一份文档切成两个 chunk，按真实管线的方式拼成全文串。"""
    cut = document.index("Accuracy")
    chunks = [
        _Chunk(document[:cut].strip(), {"char_start": 0, "char_end": len(document[:cut].strip()),
                                        "section_title": "Introduction"}),
        _Chunk(document[cut:], {"char_start": cut, "char_end": cut + len(document[cut:]),
                                "page_number": 4, "section_title": "Results"}),
    ]
    return "\n\n".join(_located_chunk_text(c, {}) for c in chunks), chunks


def test_a_candidate_span_slices_the_original_document_back_out():
    """这一条是整个 Step 3 的验收：区间切原文 == 证据原文。"""
    fulltext, chunks = _fulltext_from(DOC)
    candidates = _located_fulltext_candidates(fulltext)

    assert candidates, "should extract the numeric passages"
    checked = 0
    for candidate in candidates:
        if candidate.char_start is None:
            continue
        assert DOC[candidate.char_start : candidate.char_end] == candidate.text, candidate.text
        checked += 1
    assert checked, "至少要有一条带区间的候选"


def test_the_span_is_consistent_with_the_paragraph_and_section_locator():
    fulltext, _chunks = _fulltext_from(DOC)
    located = [c for c in _located_fulltext_candidates(fulltext) if c.char_start is not None]

    for candidate in located:
        # 区间落在它自己声称的 section 那个 chunk 里
        if candidate.section_path == "Results":
            assert candidate.char_start >= DOC.index("Accuracy")
        # paragraph_index 与区间顺序一致：同一 chunk 内，段号大的起点也大
    same_chunk = [c for c in located if c.section_path == "Results"]
    ordered = sorted(same_chunk, key=lambda c: c.paragraph_index or 0)
    starts = [c.char_start for c in ordered]
    assert starts == sorted(starts), "段号顺序必须与字符位置顺序一致"


def test_spans_do_not_overlap_between_candidates():
    fulltext, _chunks = _fulltext_from(DOC)
    located = sorted(
        (c for c in _located_fulltext_candidates(fulltext) if c.char_start is not None),
        key=lambda c: c.char_start,
    )
    for earlier, later in zip(located, located[1:], strict=False):
        assert earlier.char_end <= later.char_start, "证据区间不应互相重叠"


def test_an_old_parse_without_char_markers_still_extracts_evidence_without_spans():
    """向后兼容：库里已有的解析产物没有 CHAR 标记，抽取照旧，只是没有区间。"""
    legacy = "[[PAGE=4 | SECTION=Results]]\nAccuracy improved from 61.2% to 78.9%."
    candidates = _located_fulltext_candidates(legacy)
    assert candidates
    assert all(c.char_start is None and c.char_end is None for c in candidates)
    # locator contract 不受影响
    assert candidates[0].page == 4
    assert candidates[0].section_path == "Results"
    assert candidates[0].grade == "B_located_prose"


# --- 5. LLM 摘录路径 ------------------------------------------------------------


def test_a_model_excerpt_that_appears_verbatim_once_gets_its_span():
    fulltext, _chunks = _fulltext_from(DOC)
    excerpt = "Recall fell to 38.0% once the injection ratio dropped below 0.2 percent."
    spans = _chunk_spans(fulltext)
    start, end = _locate_excerpt(fulltext, spans, excerpt)
    assert start is not None
    assert DOC[start:end] == excerpt


def test_an_excerpt_the_model_paraphrased_gets_no_span():
    fulltext, _chunks = _fulltext_from(DOC)
    spans = _chunk_spans(fulltext)
    assert _locate_excerpt(fulltext, spans, "Recall dropped sharply at low ratios.") == (None, None)


def test_an_excerpt_that_appears_twice_gets_no_span():
    """出现多次就定不下来位置，宁可留空也不要指错。"""
    repeated = "See Table 3."
    document = f"[[CHAR=0-40]]\n{repeated} Filler prose here.\n\n[[CHAR=40-70]]\n{repeated} More."
    spans = _chunk_spans(document)
    assert _locate_excerpt(document, spans, repeated) == (None, None)


def test_the_llm_path_carries_spans_into_candidates():
    fulltext, _chunks = _fulltext_from(DOC)
    excerpt = "Accuracy improved from 61.2% to 78.9% on the held-out split of the benchmark."
    candidates = _evidence_candidates(
        fulltext=fulltext,
        abstract=None,
        quotable_points=[{"text": excerpt, "page": 4, "section": "Results", "paragraph": 1}],
        fulltext_used=True,
    )
    llm_candidate = candidates[0]
    assert llm_candidate.text == excerpt
    assert llm_candidate.char_start is not None
    assert DOC[llm_candidate.char_start : llm_candidate.char_end] == excerpt


# --- 6. 既有契约不动 ------------------------------------------------------------


def test_adding_spans_changes_no_grade_and_no_anchor_strength():
    """Step 1 的 locator contract 与定级必须逐字不变。"""
    fulltext, _chunks = _fulltext_from(DOC)
    with_spans = _located_fulltext_candidates(fulltext)
    without = _located_fulltext_candidates(re.sub(r" \| CHAR=\d+-\d+", "", fulltext))

    assert [c.grade for c in with_spans] == [c.grade for c in without]
    assert [c.anchor_strength for c in with_spans] == [c.anchor_strength for c in without]
    assert [c.page for c in with_spans] == [c.page for c in without]
    assert [c.section_path for c in with_spans] == [c.section_path for c in without]
    assert [c.paragraph_index for c in with_spans] == [c.paragraph_index for c in without]
    assert [c.text for c in with_spans] == [c.text for c in without]
