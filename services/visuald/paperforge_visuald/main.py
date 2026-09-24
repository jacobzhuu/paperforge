from __future__ import annotations

import base64
import io
import math
import statistics
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from observability.admission import AdmissionMiddleware
from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402
from visuals import ChartSpec, DiagramSpec  # noqa: E402

app = FastAPI(title="PaperForge visuald", version="0.1.0")

MAX_ROWS = 500
MAX_COLUMNS = 40
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 20_000_000
COLORBLIND = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9"]
DIAGRAM_FILLS = ["#DBEAFE", "#FFEDD5", "#DCFCE7", "#FCE7F3", "#FEF3C7", "#EDE9FE"]
DIAGRAM_PNG_DPI = 300
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["Noto Sans CJK SC", "WenQuanYi Zen Hei", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "svg.fonttype": "none",
        "svg.hashsalt": "paperforge-visuald-v1",
        "pdf.fonttype": 42,
    }
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChartData(StrictRequest):
    headers: list[str] = Field(min_length=1, max_length=MAX_COLUMNS)
    rows: list[list[Any]] = Field(max_length=MAX_ROWS)


class ChartRenderRequest(StrictRequest):
    spec: ChartSpec
    data: ChartData


class DiagramRenderRequest(StrictRequest):
    spec: DiagramSpec


class NormalizeRequest(StrictRequest):
    image_base64: str
    # AI provider 偶尔会忽略请求尺寸，甚至返回极端长宽图。worker 把原始请求尺寸
    # 一并传来，visuald 在落库前自动适配到论文画布，避免“已经计费并生成，最后
    # 却因宽高比预检失败”的死路。
    target_size: str | None = Field(
        default=None,
        pattern=r"^(1024x1024|1536x1024|1024x1536)$",
    )


app.add_middleware(AdmissionMiddleware, limit=1)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/render/chart")
def render_chart(request: ChartRenderRequest) -> dict[str, Any]:
    try:
        prepared, trace = prepare_chart_data(request.spec, request.data)
        renditions = chart_renditions(request.spec, prepared)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "renditions": renditions,
        "provenance": {
            "points": trace,
            "visual_qa": visual_qa(renditions, minimum_font_pt=8, check_outer_margin=True),
        },
    }


@app.post("/render/diagram")
def render_diagram(request: DiagramRenderRequest) -> dict[str, Any]:
    effective_direction = _diagram_direction(request.spec)
    try:
        renditions = diagram_renditions(request.spec, direction=effective_direction)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "renditions": renditions,
        "provenance": {
            "layout": "graphviz-dot",
            "requested_direction": request.spec.direction,
            "effective_direction": effective_direction,
            "visual_qa": visual_qa(
                renditions,
                minimum_font_pt=8,
                check_outer_margin=True,
                aspect_ratio_bounds=(0.2, 6.0),
            ),
        },
    }


@app.post("/normalize")
def normalize(request: NormalizeRequest) -> dict[str, Any]:
    try:
        raw = base64.b64decode(request.image_base64, validate=True)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid base64 image") from error
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image exceeds 32 MiB or is empty")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.verify()
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise HTTPException(status_code=413, detail="image pixel count exceeds limit")
            rgba = source.convert("RGBA")
            clean = Image.new("RGB", rgba.size, "white")
            clean.paste(rgba, mask=rgba.getchannel("A"))
            source_size = clean.size
            if request.target_size:
                target_width, target_height = (
                    int(value) for value in request.target_size.split("x", maxsplit=1)
                )
                if clean.size != (target_width, target_height):
                    # ImageOps.fit 保持比例后居中裁切，不会把人物/图形横向拉伸；
                    # 相比白边 padding，它也不会制造 excessive_whitespace 新错误。
                    clean = ImageOps.fit(
                        clean,
                        (target_width, target_height),
                        method=Image.Resampling.LANCZOS,
                        centering=(0.5, 0.5),
                    )
            output = io.BytesIO()
            clean.save(output, format="PNG", optimize=True, dpi=(300, 300))
            renditions = [
                _rendition("png", "image/png", output.getvalue(), clean.width, clean.height)
            ]
            return {
                "renditions": renditions,
                "provenance": {
                    "normalized": True,
                    "metadata_removed": True,
                    "source_size": list(source_size),
                    "target_size": request.target_size,
                    "canvas_adjusted": source_size != clean.size,
                    "visual_qa": visual_qa(
                        renditions,
                        minimum_font_pt=None,
                        check_outer_margin=False,
                        # AI 插图常用大面积白底和中心构图；图表的 75% 留白硬阈值
                        # 不适合照片/插画。空白图仍由 visual_content_empty 拒绝。
                        maximum_outer_whitespace=None,
                    ),
                },
            }
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise HTTPException(status_code=422, detail="unsupported or corrupt image") from error


def prepare_chart_data(spec: ChartSpec, data: ChartData) -> tuple[list[dict[str, Any]], list[dict]]:
    headers = data.headers
    if len(set(headers)) != len(headers):
        raise ValueError("dataset headers must be unique")
    required = {spec.x, *spec.y}
    required.update(filter(None, [spec.series, spec.error_lower, spec.error_upper]))
    required.update(item.column for item in spec.filters)
    missing = sorted(required - set(headers))
    if missing:
        raise ValueError(f"dataset is missing columns: {', '.join(missing)}")
    indexes = {name: headers.index(name) for name in required}
    records: list[dict[str, Any]] = []
    for row_index, row in enumerate(data.rows, start=2):
        if len(row) > len(headers):
            raise ValueError(f"row {row_index} has more cells than headers")
        record = {name: row[index] if index < len(row) else "" for name, index in indexes.items()}
        record["__row__"] = row_index
        if all(_matches_filter(record[item.column], item.op, item.value) for item in spec.filters):
            records.append(record)
    if not records:
        raise ValueError("chart filters produced no rows")
    for record in records:
        if spec.aggregation != "count":
            for column in spec.y:
                record[column] = _number(record[column], column, record["__row__"])
        for column in filter(None, [spec.error_lower, spec.error_upper]):
            record[column] = _number(record[column], column, record["__row__"])
        if spec.error_lower and spec.error_upper:
            value = record[spec.y[0]]
            if record[spec.error_lower] > value or record[spec.error_upper] < value:
                raise ValueError(f"error bounds must contain y at row {record['__row__']}")

    group_fields = [spec.x] + ([spec.series] if spec.series else [])
    if spec.aggregation != "none":
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            grouped[tuple(record[field] for field in group_fields)].append(record)
        aggregated: list[dict[str, Any]] = []
        for key, members in grouped.items():
            row = {field: value for field, value in zip(group_fields, key, strict=True)}
            row["__rows__"] = [member["__row__"] for member in members]
            for column in spec.y:
                values = [member[column] for member in members]
                row[column] = _aggregate(values, spec.aggregation)
            aggregated.append(row)
        records = aggregated
    else:
        for record in records:
            record["__rows__"] = [record["__row__"]]

    if spec.sort != "none":
        records.sort(key=lambda row: _sort_key(row[spec.x]), reverse=spec.sort == "desc")

    trace = []
    for record in records:
        for column in spec.y:
            source_cells = [
                f"{_column_name(headers.index(spec.x))}{row_no}" for row_no in record["__rows__"]
            ] + [f"{_column_name(headers.index(column))}{row_no}" for row_no in record["__rows__"]]
            if spec.error_lower and spec.error_upper:
                source_cells.extend(
                    f"{_column_name(headers.index(bound))}{row_no}"
                    for bound in (spec.error_lower, spec.error_upper)
                    for row_no in record["__rows__"]
                )
            trace.append(
                {
                    "x": record[spec.x],
                    "y": record[column],
                    "y_column": column,
                    "series": record.get(spec.series) if spec.series else None,
                    "source_cells": source_cells,
                    "aggregation": spec.aggregation,
                    "error_lower": record.get(spec.error_lower) if spec.error_lower else None,
                    "error_upper": record.get(spec.error_upper) if spec.error_upper else None,
                }
            )
    return records, trace


def chart_renditions(spec: ChartSpec, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    size = (7.0, 3.8) if spec.width == "full" else (3.35, 2.35)
    fig, ax = plt.subplots(figsize=size, constrained_layout=True)
    colors = ["#111111", "#666666", "#AAAAAA"] if spec.palette == "grayscale" else COLORBLIND
    try:
        if spec.chart_type == "box":
            # `labels=` 在 matplotlib 3.9 弃用、3.11 移除，而本服务把 matplotlib
            # 钉在 3.11.1——用旧参数名会抛 TypeError，而 render_chart 只捕 ValueError，
            # 于是箱线图返回的是裸 500 而不是干净的 422。
            ax.boxplot([[row[column] for row in records] for column in spec.y], tick_labels=spec.y)
        elif spec.chart_type == "heatmap":
            matrix = [[row[column] for column in spec.y] for row in records]
            image = ax.imshow(matrix, aspect="auto", cmap="viridis")
            ax.set_xticks(range(len(spec.y)), spec.y, rotation=30, ha="right")
            ax.set_yticks(range(len(records)), [str(row[spec.x]) for row in records])
            fig.colorbar(image, ax=ax)
        else:
            _plot_series(ax, spec, records, colors)
        ax.set_xlabel(spec.x_label or spec.x)
        ylabel = spec.y_label or (", ".join(spec.y))
        ax.set_ylabel(f"{ylabel} ({spec.unit})" if spec.unit else ylabel)
        ax.grid(axis="y", color="#D9D9D9", linewidth=0.6, alpha=0.7)
        # 只有 _plot_series 那条路径才会产出带 label 的 artist；box/heatmap 自己
        # 就把类别画在坐标轴上，对它们调 legend() 只会画空图例并抛 UserWarning。
        if spec.chart_type not in {"box", "heatmap"} and (len(spec.y) > 1 or spec.series):
            ax.legend(frameon=False, fontsize=8)
        return _save_figure(fig)
    finally:
        plt.close(fig)


def _plot_series(ax, spec: ChartSpec, records: list[dict[str, Any]], colors: list[str]) -> None:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if spec.series:
        for record in records:
            groups[str(record[spec.series])].append(record)
    else:
        groups[""] = records

    # 横轴类目：跨所有 series 去重并保序。
    #
    # 此前刻度取自 `records` 的**长度**（`labels = [... for row in records]`），
    # 而每个 series 又用组内局部下标 `range(len(xs))` 定位。两个 series × 三个 x 值
    # 因此画出六个刻度 task1,task2,task3,task1,task2,task3，后三个下面一个点都没有。
    # 刻度必须按 x 的去重有序值算，各 series 再按 x 映射到同一套位置上。
    categories: list[str] = []
    for record in records:
        label = str(record[spec.x])
        if label not in categories:
            categories.append(label)
    index_of = {label: index for index, label in enumerate(categories)}

    # numeric 判定看全部记录：按组判会出现「一个 series 落在数值轴、另一个落在类目轴」。
    numeric_x = all(isinstance(record[spec.x], (int, float)) for record in records)
    # 柱状图恒按类目排布——分组柱的 offset 是以类目宽度 1 为单位算的，
    # 放到真实数值轴上会挤成一团。
    categorical = not numeric_x or spec.chart_type == "bar"

    for series_index, (series_name, members) in enumerate(groups.items()):
        for y_index, column in enumerate(spec.y):
            color = colors[(series_index + y_index) % len(colors)]
            legend = " · ".join(part for part in [series_name, column] if part)
            ys = [row[column] for row in members]
            yerr = None
            if spec.error_lower and spec.error_upper:
                yerr = [
                    [row[column] - row[spec.error_lower] for row in members],
                    [row[spec.error_upper] - row[column] for row in members],
                ]
            positions = (
                [index_of[str(row[spec.x])] for row in members]
                if categorical
                else [row[spec.x] for row in members]
            )
            if spec.chart_type == "bar":
                width = 0.8 / max(1, len(spec.y) * len(groups))
                offset = (series_index * len(spec.y) + y_index) * width - 0.4 + width / 2
                ax.bar(
                    [position + offset for position in positions],
                    ys,
                    width=width,
                    label=legend,
                    color=color,
                    yerr=yerr,
                    capsize=2 if yerr else 0,
                )
            elif spec.chart_type == "scatter":
                if yerr:
                    ax.errorbar(
                        positions,
                        ys,
                        yerr=yerr,
                        fmt="o",
                        linestyle="none",
                        label=legend,
                        color=color,
                        markersize=4,
                        capsize=2,
                    )
                else:
                    ax.scatter(positions, ys, label=legend, color=color, s=24)
            else:
                if yerr:
                    ax.errorbar(
                        positions,
                        ys,
                        yerr=yerr,
                        marker="o",
                        linewidth=1.5,
                        markersize=3.5,
                        label=legend,
                        color=color,
                        capsize=2,
                    )
                else:
                    ax.plot(
                        positions,
                        ys,
                        marker="o",
                        linewidth=1.5,
                        markersize=3.5,
                        label=legend,
                        color=color,
                    )

    # 刻度在循环外设一次。此前写在最内层，每个 series × 每个 y 列都重设一遍。
    if categorical:
        ax.set_xticks(range(len(categories)), categories, rotation=30, ha="right")


def diagram_renditions(
    spec: DiagramSpec,
    *,
    direction: str | None = None,
) -> list[dict[str, Any]]:
    dot = _diagram_dot(spec, direction=direction)
    renditions = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "diagram.dot"
        source.write_text(dot, encoding="utf-8")
        for fmt, media_type in (
            ("svg", "image/svg+xml"),
            ("pdf", "application/pdf"),
            ("png", "image/png"),
        ):
            target = root / f"diagram.{fmt}"
            command = ["dot", f"-T{fmt}"]
            if fmt == "png":
                # Graphviz otherwise rasterizes at roughly 96 DPI. Short horizontal
                # diagrams then end up only 70-150 px high and are rejected by the
                # publication preflight even though the SVG/PDF is perfectly valid.
                command.append(f"-Gdpi={DIAGRAM_PNG_DPI}")
            command.extend([str(source), "-o", str(target)])
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc.returncode != 0 or not target.exists():
                raise ValueError(f"graphviz failed: {proc.stderr[:200]}")
            width = height = None
            if fmt == "png":
                with Image.open(target) as image:
                    width, height = image.size
            renditions.append(_rendition(fmt, media_type, target.read_bytes(), width, height))
    return renditions


def visual_qa(
    renditions: list[dict[str, Any]],
    *,
    minimum_font_pt: int | None,
    check_outer_margin: bool,
    minimum_size: tuple[int, int] = (240, 160),
    aspect_ratio_bounds: tuple[float, float] = (0.25, 4.0),
    maximum_outer_whitespace: float | None = 0.75,
) -> dict[str, Any]:
    """Preflight dimensions, aspect ratio, content bounds, whitespace and font floor."""
    issues: list[dict[str, Any]] = []
    png = next((item for item in renditions if item.get("format") == "png"), None)
    metrics: dict[str, Any] = {"minimum_font_pt": minimum_font_pt}
    if png is None:
        issues.append({"code": "png_rendition_missing", "message": "缺少 PNG 预检版本"})
    else:
        raw = base64.b64decode(str(png["data_base64"]), validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            metrics.update({"width": width, "height": height})
            aspect_ratio = width / max(1, height)
            metrics["aspect_ratio"] = round(aspect_ratio, 4)
            minimum_width, minimum_height = minimum_size
            if width < minimum_width or height < minimum_height:
                issues.append({"code": "visual_resolution_too_small", "message": "视觉分辨率过低"})
            minimum_aspect, maximum_aspect = aspect_ratio_bounds
            if not minimum_aspect <= aspect_ratio <= maximum_aspect:
                issues.append({"code": "visual_aspect_ratio_extreme", "message": "视觉宽高比异常"})
            white = Image.new("RGB", rgb.size, "white")
            bbox = ImageChops.difference(rgb, white).getbbox()
            if bbox is None:
                issues.append({"code": "visual_content_empty", "message": "视觉内容为空"})
            else:
                x0, y0, x1, y1 = bbox
                content_area = max(0, x1 - x0) * max(0, y1 - y0)
                outer_whitespace = 1 - content_area / max(1, width * height)
                margins = [x0, y0, width - x1, height - y1]
                metrics.update(
                    {
                        "content_bbox": [x0, y0, x1, y1],
                        "outer_whitespace_ratio": round(outer_whitespace, 4),
                        "minimum_outer_margin_px": min(margins),
                    }
                )
                if (
                    maximum_outer_whitespace is not None
                    and outer_whitespace > maximum_outer_whitespace
                ):
                    issues.append(
                        {"code": "visual_excessive_whitespace", "message": "视觉外围留白过多"}
                    )
                if check_outer_margin and min(margins) < 2:
                    issues.append(
                        {"code": "visual_content_clipped", "message": "视觉内容触及画布边缘"}
                    )
    return {"passed": not issues, "issues": issues, "metrics": metrics}


def _diagram_direction(spec: DiagramSpec) -> str:
    # A left-to-right chain is readable at full page width, but pathological in a
    # single column (the real failure was 994×77, aspect ratio 12.9:1). Preserve
    # the requested direction in provenance while choosing a printable layout.
    return "TB" if spec.width == "column" and spec.direction == "LR" else spec.direction


def _diagram_dot(spec: DiagramSpec, *, direction: str | None = None) -> str:
    lines = [
        "digraph PaperForge {",
        f"rankdir={direction or spec.direction};",
        'graph [bgcolor="white", pad="0.15", nodesep="0.35", ranksep="0.5"];',
        'node [fontname="Noto Sans CJK SC", fontsize=10, color="#4B5563", '
        'style="filled", fillcolor="#F8FAFC", penwidth=1.2];',
        'edge [fontname="Noto Sans CJK SC", fontsize=8, color="#64748B", arrowsize=0.7];',
    ]
    grouped = {group.id: [] for group in spec.groups}
    ungrouped = []
    for node in spec.nodes:
        (grouped[node.group] if node.group else ungrouped).append(node)
    group_labels = {group.id: group.label for group in spec.groups}
    for group_id, nodes in grouped.items():
        lines.extend(
            [
                # 整个名字包在一对引号里，不能写成 `cluster_{_dot_id(id)}`——那会生成
                # `subgraph cluster_"enc"`，在 DOT 文法里是「ID 后面又跟了一个 ID」，
                # graphviz 直接报 syntax error，任何带分组的示意图都渲染不出来。
                f'subgraph "cluster_{group_id}" {{',
                f'label="{_dot_text(group_labels[group_id])}";',
                'color="#CBD5E1"; style="rounded";',
            ]
        )
        lines.extend(
            _dot_node(node, fillcolor=DIAGRAM_FILLS[index % len(DIAGRAM_FILLS)])
            for index, node in enumerate(nodes)
        )
        lines.append("}")
    lines.extend(
        _dot_node(node, fillcolor=DIAGRAM_FILLS[index % len(DIAGRAM_FILLS)])
        for index, node in enumerate(ungrouped)
    )
    for edge in spec.edges:
        label = f' [label="{_dot_text(edge.label)}"]' if edge.label else ""
        lines.append(f"{_dot_id(edge.source)} -> {_dot_id(edge.target)}{label};")
    lines.append("}")
    return "\n".join(lines)


def _dot_node(node, *, fillcolor: str) -> str:
    shape = {"box": "box", "rounded": "box", "ellipse": "ellipse", "diamond": "diamond"}[node.shape]
    style = 'style="rounded,filled"' if node.shape == "rounded" else 'style="filled"'
    return (
        f'{_dot_id(node.id)} [label="{_dot_text(node.label)}", shape={shape}, '
        f'{style}, fillcolor="{fillcolor}"];'
    )


def _dot_id(value: str) -> str:
    return f'"{value}"'


def _dot_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _save_figure(fig) -> list[dict[str, Any]]:
    outputs = []
    for fmt, media_type in (
        ("svg", "image/svg+xml"),
        ("pdf", "application/pdf"),
        ("png", "image/png"),
    ):
        buffer = io.BytesIO()
        metadata = (
            {"Date": None}
            if fmt == "svg"
            else ({"CreationDate": None, "ModDate": None} if fmt == "pdf" else {})
        )
        fig.savefig(buffer, format=fmt, dpi=300, facecolor="white", metadata=metadata)
        width = height = None
        if fmt == "png":
            with Image.open(io.BytesIO(buffer.getvalue())) as image:
                width, height = image.size
        outputs.append(_rendition(fmt, media_type, buffer.getvalue(), width, height))
    return outputs


def _rendition(fmt: str, media_type: str, data: bytes, width=None, height=None) -> dict[str, Any]:
    return {
        "format": fmt,
        "media_type": media_type,
        "data_base64": base64.b64encode(data).decode("ascii"),
        "width": width,
        "height": height,
    }


def _number(value: Any, column: str, row: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"column {column} contains a non-numeric value at row {row}") from error
    if not math.isfinite(number):
        raise ValueError(f"column {column} contains a non-finite value at row {row}")
    return number


def _aggregate(values: list[float], method: str) -> float:
    if method == "mean":
        return statistics.fmean(values)
    if method == "median":
        return statistics.median(values)
    if method == "sum":
        return sum(values)
    if method == "count":
        return float(len(values))
    raise ValueError(f"unsupported aggregation: {method}")


def _matches_filter(actual: Any, op: str, expected: Any) -> bool:
    if op == "in":
        # 逐项复用 eq 的语义，而不是 `actual in expected`。
        #
        # eq 有字符串兜底比较（CSV 把数值列解析成 "1" 时仍能匹配过滤值 1），
        # 而裸 `in` 是精确相等：同一份数据 `eq 1` 命中、`in [1, 2]` 却漏掉。
        # in 就是 eq 的列表版，两者的匹配口径必须一致。
        return any(_matches_filter(actual, "eq", item) for item in expected)
    if op == "eq":
        return actual == expected or str(actual) == str(expected)
    if op == "ne":
        return not _matches_filter(actual, "eq", expected)
    try:
        left, right = float(actual), float(expected)
    except (TypeError, ValueError):
        left, right = str(actual), str(expected)
    return {"lt": left < right, "lte": left <= right, "gt": left > right, "gte": left >= right}[op]


def _sort_key(value: Any) -> tuple[int, Any]:
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, str(value))


def _column_name(index: int) -> str:
    result = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result
