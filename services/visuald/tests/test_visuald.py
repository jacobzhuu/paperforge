from __future__ import annotations

import base64
import io
import re
import shutil

import pytest
from fastapi.testclient import TestClient
from paperforge_visuald.main import _diagram_dot, app
from PIL import Image
from visuals import DiagramSpec


def test_chart_renders_three_formats_with_cell_trace() -> None:
    response = TestClient(app).post(
        "/render/chart",
        json={
            "spec": {
                "kind": "chart",
                "chart_type": "bar",
                "source_asset_ref": "ua_deadbeef",
                "x": "method",
                "y": ["score"],
                "x_label": "Method",
                "y_label": "Score",
            },
            "data": {
                "headers": ["method", "score"],
                "rows": [["A", "0.8"], ["B", "0.9"]],
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["format"] for item in body["renditions"]} == {"svg", "pdf", "png"}
    assert body["provenance"]["points"][0]["source_cells"] == ["A2", "B2"]
    assert body["provenance"]["visual_qa"]["passed"] is True
    assert body["provenance"]["visual_qa"]["metrics"]["minimum_font_pt"] == 8
    assert base64.b64decode(
        next(item["data_base64"] for item in body["renditions"] if item["format"] == "png")
    ).startswith(b"\x89PNG")


def test_chart_rejects_non_numeric_y_without_sampling_or_imputation() -> None:
    response = TestClient(app).post(
        "/render/chart",
        json={
            "spec": {
                "kind": "chart",
                "chart_type": "line",
                "source_asset_ref": "ua_deadbeef",
                "x": "epoch",
                "y": ["score"],
            },
            "data": {"headers": ["epoch", "score"], "rows": [[1, "missing"]]},
        },
    )
    assert response.status_code == 422
    assert "non-numeric" in response.text


def test_chart_error_bounds_are_rendered_and_traced() -> None:
    response = TestClient(app).post(
        "/render/chart",
        json={
            "spec": {
                "kind": "chart",
                "chart_type": "line",
                "source_asset_ref": "ua_deadbeef",
                "x": "epoch",
                "y": ["score"],
                "error_lower": "low",
                "error_upper": "high",
            },
            "data": {
                "headers": ["epoch", "score", "low", "high"],
                "rows": [[1, 0.8, 0.75, 0.84], [2, 0.9, 0.88, 0.93]],
            },
        },
    )
    assert response.status_code == 200, response.text
    point = response.json()["provenance"]["points"][0]
    assert point["source_cells"] == ["A2", "B2", "C2", "D2"]
    assert point["error_lower"] == 0.75
    assert point["error_upper"] == 0.84


def test_chart_rejects_error_bounds_that_do_not_contain_y() -> None:
    response = TestClient(app).post(
        "/render/chart",
        json={
            "spec": {
                "kind": "chart",
                "chart_type": "bar",
                "source_asset_ref": "ua_deadbeef",
                "x": "method",
                "y": ["score"],
                "error_lower": "low",
                "error_upper": "high",
            },
            "data": {
                "headers": ["method", "score", "low", "high"],
                "rows": [["A", 0.8, 0.85, 0.9]],
            },
        },
    )
    assert response.status_code == 422
    assert "must contain y" in response.text


def test_normalize_removes_metadata_and_returns_png() -> None:
    source = io.BytesIO()
    Image.new("RGBA", (4, 3), (255, 0, 0, 128)).save(source, format="PNG", pnginfo=None)
    response = TestClient(app).post(
        "/normalize",
        json={"image_base64": base64.b64encode(source.getvalue()).decode()},
    )
    assert response.status_code == 200
    item = response.json()["renditions"][0]
    assert item["format"] == "png"
    assert (item["width"], item["height"]) == (4, 3)
    assert response.json()["provenance"]["visual_qa"]["passed"] is False


def test_normalize_rejects_invalid_base64_corrupt_and_oversize(monkeypatch) -> None:
    import paperforge_visuald.main as visuald_main

    client = TestClient(app)
    assert client.post("/normalize", json={"image_base64": "%%%"}).status_code == 422
    corrupt = base64.b64encode(b"%PDF-1.7 not an image").decode()
    assert client.post("/normalize", json={"image_base64": corrupt}).status_code == 422

    monkeypatch.setattr(visuald_main, "MAX_IMAGE_BYTES", 4)
    oversized = base64.b64encode(b"12345").decode()
    assert client.post("/normalize", json={"image_base64": oversized}).status_code == 413


def _svg_of(body: dict) -> str:
    return base64.b64decode(
        next(item["data_base64"] for item in body["renditions"] if item["format"] == "svg")
    ).decode("utf-8")


def _axis_ticks(svg: str, prefix: str) -> list[str]:
    return [
        text for text in re.findall(r"<text[^>]*>([^<]+)</text>", svg) if text.startswith(prefix)
    ]


def test_every_advertised_chart_type_renders() -> None:
    """五种图形都要能出图。

    box 曾经恒定 500：`ax.boxplot(labels=...)` 在 matplotlib 3.9 弃用、3.11 移除，
    而本服务把 matplotlib 钉在 3.11.1，抛出的 TypeError 又不在 render_chart 捕获的
    ValueError 里，于是用户拿到裸 500。当时 box 与 heatmap 都没有任何测试。
    """
    client = TestClient(app)
    data = {
        "headers": ["model", "acc", "f1"],
        "rows": [["A", 0.91, 0.88], ["B", 0.87, 0.85], ["C", 0.93, 0.90]],
    }
    for chart_type in ("bar", "line", "scatter", "box", "heatmap"):
        response = client.post(
            "/render/chart",
            json={
                "spec": {
                    "kind": "chart",
                    "chart_type": chart_type,
                    "source_asset_ref": "ua_deadbeef",
                    "x": "model",
                    "y": ["acc", "f1"],
                },
                "data": data,
            },
        )
        assert response.status_code == 200, f"{chart_type}: {response.text}"
        assert {item["format"] for item in response.json()["renditions"]} == {"svg", "pdf", "png"}


def test_multi_series_axis_has_one_tick_per_distinct_x() -> None:
    """多 series 的横轴刻度按 x 去重，不按记录条数。

    此前刻度取自 `records` 的长度、而每个 series 用组内局部下标定位：
    2 个 series × 3 个 x 值会画出 6 个刻度（task1,task2,task3,task1,task2,task3），
    后三个下面一个数据点都没有。
    """
    rows = [
        [task, series, value]
        for series in ("BERT", "GPT")
        for task, value in (("task1", 0.7), ("task2", 0.8), ("task3", 0.9))
    ]
    client = TestClient(app)
    for chart_type in ("line", "bar", "scatter"):
        response = client.post(
            "/render/chart",
            json={
                "spec": {
                    "kind": "chart",
                    "chart_type": chart_type,
                    "source_asset_ref": "ua_deadbeef",
                    "x": "task",
                    "y": ["acc"],
                    "series": "model",
                    "x_label": "Task",
                },
                "data": {"headers": ["task", "model", "acc"], "rows": rows},
            },
        )
        assert response.status_code == 200, response.text
        ticks = _axis_ticks(_svg_of(response.json()), "task")
        assert ticks == ["task1", "task2", "task3"], f"{chart_type} 的刻度是 {ticks}"


def test_grouped_diagram_emits_parsable_dot() -> None:
    """带分组的示意图必须生成合法 DOT。

    `f"subgraph cluster_{_dot_id(id)}"` 会产出 `subgraph cluster_"enc"`——在 DOT 文法里
    是「一个 ID 后面又跟了一个 ID」，graphviz 报 syntax error，于是**任何**带 groups 的
    示意图都渲染不出来。这个断言不依赖 graphviz，所以在没装 dot 的开发机上也拦得住。
    """
    spec = DiagramSpec.model_validate(
        {
            "kind": "diagram",
            "nodes": [
                {"id": "enc", "label": "编码器", "group": "stage1"},
                {"id": "dec", "label": "解码器", "group": "stage2"},
            ],
            "edges": [{"source": "enc", "target": "dec", "label": "隐状态"}],
            "groups": [
                {"id": "stage1", "label": "编码阶段"},
                {"id": "stage2", "label": "解码阶段"},
            ],
        }
    )
    dot = _diagram_dot(spec)
    assert 'subgraph "cluster_stage1" {' in dot
    assert 'subgraph "cluster_stage2" {' in dot
    # 旧的坏形态：cluster_ 之后紧跟一个引号。
    assert 'cluster_"' not in dot


@pytest.mark.skipif(shutil.which("dot") is None, reason="graphviz 未安装")
def test_grouped_diagram_renders_all_formats() -> None:
    """真正调用 graphviz 渲染带分组的示意图（CI 与 visuald 镜像里都装了 dot）。"""
    response = TestClient(app).post(
        "/render/diagram",
        json={
            "spec": {
                "kind": "diagram",
                "direction": "LR",
                "nodes": [
                    {"id": "enc", "label": "编码器", "group": "stage1"},
                    {"id": "dec", "label": "解码器"},
                ],
                "edges": [{"source": "enc", "target": "dec", "label": "隐状态"}],
                "groups": [{"id": "stage1", "label": "编码阶段"}],
            }
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["format"] for item in body["renditions"]} == {"svg", "pdf", "png"}
    svg = _svg_of(body)
    # 分组标签与中文都要真的进入产物。
    assert "编码阶段" in svg
    assert 'font-family="Noto Sans CJK SC"' in svg


def test_in_filter_matches_the_same_rows_as_eq() -> None:
    """`in` 是 `eq` 的列表版，匹配口径必须一致。

    CSV/XLSX 解析常把数值列留成字符串。`eq` 有字符串兜底比较所以能命中，
    而 `in` 此前是裸 `actual in expected` 的精确相等：同一份数据同一个值，
    `eq 1` 命中、`in [1, 2]` 却静默漏掉，图上少画点且不报错。
    """
    client = TestClient(app)
    # epoch 列是字符串（CSV 常态），过滤值是数值。
    data = {
        "headers": ["epoch", "score"],
        "rows": [["1", 0.5], ["2", 0.7], ["3", 0.9]],
    }
    base_spec = {
        "kind": "chart",
        "chart_type": "line",
        "source_asset_ref": "ua_deadbeef",
        "x": "epoch",
        "y": ["score"],
    }

    def points(filters: list[dict]) -> list:
        response = client.post(
            "/render/chart",
            json={"spec": {**base_spec, "filters": filters}, "data": data},
        )
        assert response.status_code == 200, response.text
        return [point["x"] for point in response.json()["provenance"]["points"]]

    assert points([{"column": "epoch", "op": "eq", "value": 1}]) == ["1"]
    # 同样的值走 in，必须命中同一行。
    assert points([{"column": "epoch", "op": "in", "value": [1, 2]}]) == ["1", "2"]
    # 字符串形式的过滤值当然也要命中。
    assert points([{"column": "epoch", "op": "in", "value": ["1", "2"]}]) == ["1", "2"]
