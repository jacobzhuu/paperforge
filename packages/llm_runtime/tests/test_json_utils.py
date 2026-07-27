from llm_runtime import CiteKeyViolation, clean_and_parse_json, purify_llm_json


def test_clean_and_parse_strips_markdown_fence():
    text = '```json\n{"a": 1}\n```'
    assert clean_and_parse_json(text) == {"a": 1}


def test_clean_and_parse_extracts_object_from_prose():
    text = 'Here is the result: {"a": [1, 2]} hope it helps.'
    assert clean_and_parse_json(text) == {"a": [1, 2]}


def test_purify_reports_cite_key_violation_without_mutating_first_pass():
    raw = '{"runs": [{"cite_keys": ["wang2023survey", "ghost2099fake"]}]}'
    parsed, violations = purify_llm_json(raw, allowed_cite_keys={"wang2023survey"})
    assert parsed["runs"][0]["cite_keys"] == ["wang2023survey", "ghost2099fake"]
    assert violations == [
        CiteKeyViolation(
            path="runs[0].cite_keys",
            rejected_keys=("ghost2099fake",),
        )
    ]


def test_purify_strip_removes_violation_and_still_reports_it():
    raw = '{"sections": [{"runs": [{"cite_keys": ["ok", "ghost"]}]}]}'
    parsed, violations = purify_llm_json(
        raw,
        allowed_cite_keys={"ok"},
        mode="strip",
    )
    assert parsed["sections"][0]["runs"][0]["cite_keys"] == ["ok"]
    assert violations[0].path == "sections[0].runs[0].cite_keys"


def test_purify_audits_paper_ir_cite_run_keys_but_not_generic_keys():
    raw = '{"runs":[{"t":"cite","keys":["ok","ghost"]}],"metadata":{"keys":["free"]}}'
    parsed, violations = purify_llm_json(raw, allowed_cite_keys={"ok"}, mode="strip")
    assert parsed["runs"][0]["keys"] == ["ok"]
    assert parsed["metadata"]["keys"] == ["free"]
    assert violations[0].path == "runs[0].keys"


def test_purify_without_whitelist_keeps_all_and_reports_none():
    parsed, violations = purify_llm_json('{"cite_keys": ["a", "b"]}')
    assert parsed["cite_keys"] == ["a", "b"]
    assert violations == []
