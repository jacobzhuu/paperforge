import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from llm_runtime.runner import JsonResult
from paperforge_worker.pipelines.quality import _PLACEHOLDER_RE
from paperforge_worker.pipelines.review_inputs import build_review_input
from paperforge_worker.pipelines.semantic_review import review_section


def inputs(evidence=None, ids=None, cites=None, refs=None):
    body = {
        "blocks": [
            {
                "type": "paragraph",
                "runs": [
                    {"t": "text", "v": "The reported accuracy increased by 27 percent."},
                    {"t": "cite", "keys": cites or ["paper"], "evidence_ids": ids or ["e19"]},
                    {"t": "grounding", "source_refs": refs or []},
                ],
            }
        ]
    }
    units = (
        evidence
        if evidence is not None
        else [
            {"id": f"e{i}", "work_id": "w", "text": "x" * 720 + f" support-{i}"} for i in range(21)
        ]
    )
    return build_review_input(
        section_key="s",
        question="What is supported?",
        prose="Full prose",
        body=body,
        evidence=units,
        whitelist={"paper": "w"},
        assets=[{"_asset_id": "id", "_asset_ref": "ua_id", "text": "data"}],
    )


def test_actual_binding_outside_first_fourteen_keeps_full_text_and_identity():
    one = inputs()
    assert [e["evidence_id"] for e in one["evidence"]] == ["e19"]
    assert "support-19" in one["evidence"][0]["text"]
    assert one["coverage"] == {"bound_ids": 1, "resolved_ids": 1, "binding_issues": 0}
    assert inputs() == one
    duplicate = [{"id": k, "work_id": "w", "text": "identical"} for k in ("e18", "e19")]
    assert len(inputs(duplicate, ids=["e18", "e19"])["evidence"]) == 2
    assert inputs(list(reversed(duplicate)), ids=["e18", "e19"]) == inputs(
        duplicate, ids=["e18", "e19"]
    )


@pytest.mark.parametrize("units", [[], [{"id": "e19", "work_id": "other", "text": "match"}]])
def test_missing_foreign_or_mismatched_evidence_cannot_be_rebound(units):
    payload = inputs(units)
    assert payload["evidence"] == []
    assert payload["binding_issues"][0]["reason"] == "missing_or_mismatched_binding"


def test_source_refs_resolve_only_project_assets():
    payload = inputs(refs=["ua_id", "foreign"])
    assert payload["assets"][0]["_asset_id"] == "id"
    assert any(i.get("source_ref") == "foreign" for i in payload["binding_issues"])


@pytest.mark.parametrize(
    "text,placeholder",
    [
        ("相对效果量仍待补充。", False),
        ("相关数值待补充，故不报告大小。", False),
        ("TODO: complete result", True),
        ("[待补证据]", True),
        ("待补充", True),
        ("本节尚无满足定位与可比性要求的证据；待补充可核验来源。", True),
        ("结果待实验补充", True),
        ("The Todoist service is unrelated.", False),
    ],
)
def test_placeholder_distinguishes_gap_prose(text, placeholder):
    assert bool(_PLACEHOLDER_RE.search(text)) == placeholder


async def test_complete_prompt_artifact_and_oversize_unassessed(tmp_path):
    from paperforge_worker.config import WorkerSettings
    from storage import make_object_store

    class Runner:
        enabled = True
        calls = []

        async def agenerate_json(self, *args, **kwargs):
            self.calls.append(kwargs)
            material = json.loads(kwargs["user_prompt"].split("\n", 1)[1])
            return JsonResult(
                value={
                    "answers_question": "full",
                    "support": "sufficient",
                    "calibration": "matched",
                    "synthesis_mode": "synthesized",
                    "diagnosis": "none",
                    "gap_declared": False,
                    "claim_checks": [
                        {
                            "claim_id": c["claim_id"],
                            "status": "supported",
                            "evidence_ids": c["resolved_evidence_ids"],
                            "reason": "The excerpt reports the result.",
                        }
                        for c in material["claims"]
                    ],
                    "unsupported_claims": [],
                }
            )

    events = []

    async def emit(name, payload):
        events.append((name, payload))

    context = SimpleNamespace(project_id="p", settings=WorkerSettings(), emit=emit)
    runner = Runner()
    material = inputs()
    verdict = await review_section(
        section_key="s",
        question="Q",
        prose="P",
        evidence=[],
        review_input=material,
        runner=runner,
        trace_context=context,
    )
    assert verdict is not None
    artifact = events[0][1]["artifact_key"]
    stored = json.loads(make_object_store(context.settings).get(artifact))
    assert stored["review_input"] == material
    assert stored["user_prompt"] == runner.calls[0]["user_prompt"]
    assert stored["system_prompt"] == runner.calls[0]["system_prompt"]
    assert runner.calls[0]["metadata"]["review_input_hash"] == material["input_hash"]
    assert "support-19" in runner.calls[0]["user_prompt"]
    oversized = deepcopy(material)
    oversized["prose"] = "x" * 64001
    assert (
        await review_section(
            section_key="s",
            question="Q",
            prose="P",
            evidence=[],
            review_input=oversized,
            runner=runner,
            trace_context=context,
        )
        is None
    )
    assert len(runner.calls) == 1
    assert events[-1][1]["reason"] == "input_budget_exceeded"


def test_short_claim_binding_and_isolated_placeholder_block():
    from paperforge_worker.pipelines.quality import placeholder_sections

    body = {
        "blocks": [
            {
                "type": "paragraph",
                "runs": [
                    {"t": "text", "v": "提高27%。"},
                    {"t": "cite", "keys": ["paper"], "evidence_ids": ["e"]},
                ],
            }
        ]
    }
    result = build_review_input(
        section_key="s",
        question="Q",
        prose="提高27%。",
        body=body,
        evidence=[{"id": "e", "work_id": "w", "text": "27%"}],
        whitelist={"paper": "w"},
    )
    assert result["coverage"]["resolved_ids"] == 1
    body["blocks"].append({"type": "paragraph", "runs": [{"t": "text", "v": "待补充"}]})
    assert placeholder_sections([SimpleNamespace(section_key="s", body_ir_json=body)]) == ["s"]
