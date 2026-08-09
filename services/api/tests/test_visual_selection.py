"""视觉意图自动判型的纯函数契约，不依赖 PostgreSQL。"""

from paperforge_api.routers.visuals import _select_draft_kind


def test_auto_prefers_ai_generation_for_process_intent_when_available() -> None:
    kind, reason, warnings = _select_draft_kind(
        "auto",
        "展示检索、筛选和证据整合流程",
        chart_asset=None,
        ai_images_available=True,
    )
    assert kind == "ai_image"
    assert "AI 生成" in reason
    assert warnings == []


def test_explicit_diagram_still_uses_local_deterministic_rendering() -> None:
    kind, _, _ = _select_draft_kind(
        "diagram",
        "展示检索、筛选和证据整合流程",
        chart_asset=None,
        ai_images_available=True,
    )
    assert kind == "diagram"


def test_auto_never_sends_unsourced_quantitative_chart_to_image_model() -> None:
    kind, _, warnings = _select_draft_kind(
        "auto",
        "比较不同方法的性能和结果差异",
        chart_asset=None,
        ai_images_available=True,
    )
    assert kind == "diagram"
    assert any("避免 AI 伪造数据" in warning for warning in warnings)
