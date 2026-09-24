import json
from copy import deepcopy

import pytest
from llm_runtime.runner import JsonResult
from paperforge_worker.pipelines.writing import SectionDraft, WritingContext, coherence_pass


@pytest.mark.parametrize(
    "change,accepted",
    [
        ("swap", False),
        ("missing_id", False),
        ("drop_number", False),
        ("wording", True),
    ],
)
async def test_coherence_preserves_each_sentence_binding(change, accepted):
    sentences = [
        {
            "text": "Study A reports 42 samples.",
            "cite_keys": ["a"],
            "evidence_ids": ["e1"],
            "source_refs": [],
        },
        {
            "text": "Study B describes a limited design.",
            "cite_keys": ["b"],
            "evidence_ids": ["e2"],
            "source_refs": [],
        },
    ]
    draft = SectionDraft(
        section_key="s",
        title="S",
        paragraphs=[
            {
                "text": " ".join(s["text"] for s in sentences),
                "cite_keys": ["a", "b"],
                "sentences": deepcopy(sentences),
            }
        ],
    )

    class Runner:
        enabled = True

        async def agenerate_json(self, *args, **kwargs):
            payload = json.loads(kwargs["user_prompt"].split("Current paragraphs JSON:\n")[1])
            rows = payload["paragraphs"][0]["sentences"]
            if change == "swap":
                rows[0]["evidence_ids"], rows[1]["evidence_ids"] = ["e2"], ["e1"]
            elif change == "missing_id":
                del rows[0]["sentence_id"]
            elif change == "drop_number":
                rows[0]["text"] = "Study A reports samples."
            else:
                rows[0]["text"] = "Study A describes 42 samples."
            return JsonResult(value=payload)

    result = await coherence_pass(
        draft=draft, context=WritingContext(outline={}), whitelist={"a", "b"}, runner=Runner()
    )
    assert result.generation["polish_result"]["accepted"] == accepted
    assert result.generation["polish_result"]["changed"] == accepted
    assert bool(result.generation["polish_result"]["reason"]) != accepted
    after = result.paragraphs[0]["sentences"]
    assert (after[0]["text"] != sentences[0]["text"]) == accepted
    assert [s["evidence_ids"] for s in after] == [["e1"], ["e2"]]
