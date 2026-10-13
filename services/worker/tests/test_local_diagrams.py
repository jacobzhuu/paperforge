"""Exercise summary planning, durable assets and rendering without image providers."""

from unittest.mock import Mock

import pytest
from db import get_project, list_visuals
from paperforge_worker.config import WorkerSettings
from paperforge_worker.pipelines import visuals
from test_background_revision import seed
from test_visual_planner import _Runner, _summary_payload
from visuals import RenderResult, Rendition


@pytest.mark.parametrize("render_fails", [False, True])
async def test_summary_uses_local_renderer_and_retains_failure(
    session_factory, monkeypatch, tmp_path, render_fails
):
    context, _, _ = await seed(session_factory)
    context.settings = WorkerSettings(
        ai_images_enabled=False,
        storage_backend="filesystem",
        storage_fs_root=str(tmp_path),
    )
    async with context.session() as session:
        context.owner_id = (await get_project(session, context.project_id)).owner_id
    payload = _summary_payload()
    payload["proposals"][0].update(target_section_key="s1", source_section_keys=["s1"])
    runner = _Runner(payload)
    context.llm_runner = lambda: runner
    context.warn = Mock()
    provider = Mock(side_effect=AssertionError("No external image provider"))
    monkeypatch.setattr(visuals, "create_image_provider", provider)
    render = Mock()
    if render_fails:
        render.side_effect = ValueError("graphviz failed: test failure")
    else:
        render.return_value = RenderResult(
            renditions=[
                Rendition(fmt, f"fixture-{fmt}".encode(), mime)
                for fmt, mime in [
                    ("svg", "image/svg+xml"),
                    ("png", "image/png"),
                    ("pdf", "application/pdf"),
                ]
            ],
            provenance={"visual_qa": {"passed": True}},
        )
    monkeypatch.setattr(visuals.VisualdClient, "render_diagram_result", render)
    result = await visuals.suggest_visuals(context, summary_only=True, auto_generate=True)
    assert result.diagram_count == 1 and result.ai_image_count == 0
    assert result.preview_failed_count == int(render_fails)
    assert result.auto_generated_count == int(not render_fails)
    provider.assert_not_called()
    render.assert_called_once()
    async with context.session() as session:
        rows = await list_visuals(session, context.project_id)
        assert len(rows) == 1 and rows[0].kind == "diagram"
        assert rows[0].generation_status == ("failed" if render_fails else "ready")
        if render_fails:
            assert rows[0].error_message
        else:
            assert {"svg", "png", "pdf"} <= rows[0].renditions_json.keys()
    if not render_fails:
        await visuals.suggest_visuals(context, summary_only=True, auto_generate=True)
        render.assert_called_once()  # A ready asset is never rendered again.


async def test_invalid_summary_reports_reason_without_creating_generic_diagram(
    session_factory,
    monkeypatch,
):
    context, _, _ = await seed(session_factory)
    context.settings = WorkerSettings(ai_images_enabled=False)
    context.llm_runner = lambda: _Runner({"proposals": []})
    context.warn = Mock()
    result = await visuals.suggest_visuals(context, summary_only=True, auto_generate=True)
    assert result.proposed == 0 and result.generation_deferred_count == 1
    assert "正文不足" in context.warn.call_args.args[2]["message"]
    async with context.session() as session:
        assert await list_visuals(session, context.project_id) == []
