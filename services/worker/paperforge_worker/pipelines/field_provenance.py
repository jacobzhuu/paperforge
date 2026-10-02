"""Source-region and field provenance; missing proof never acquires comparison eligibility."""

from __future__ import annotations

import re

VERSION = "experiment-v4-provenance"
FIELDS = (
    "metric_name",
    "value",
    "unit",
    "dataset",
    "split",
    "model_family",
    "baseline_name",
    "baseline_value",
    "ci_low",
    "ci_high",
    "std",
    "sample_size",
    "task",
)
REQUIRED_COMPARISON = ("metric_name", "value", "unit", "dataset", "split", "model_family", "task")
ALIASES = {
    "accuracy": ("accuracy", "acc", "准确率"),
    "test": ("test", "测试集"),
    "validation": ("validation", "验证集"),
    "%": ("%", "percent", "百分比"),
}


def collapsed(value):
    return " ".join(str(value or "").split()).casefold()


def region_contains(fulltext, marker, quote, parse_locator, locators):
    requested = (
        parse_locator(str(marker).strip().removeprefix("[[").removesuffix("]]")) if marker else {}
    )
    if not requested:
        return False

    def same(actual):
        keys = [
            k
            for k in ("page", "section", "object_ref", "paragraph")
            if requested.get(k) is not None
        ]
        return bool(keys) and all(collapsed(actual.get(k)) == collapsed(requested[k]) for k in keys)

    matches = list(re.finditer(r"\[\[[^\]]+\]\]", fulltext))
    for i, match in enumerate(matches):
        if same(parse_locator(match.group()[2:-2])):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(fulltext)
            if collapsed(quote) in collapsed(fulltext[match.end() : end]):
                return True
    return any(
        same(r) and collapsed(quote) in collapsed(r.get("text"))
        for r in getattr(locators, "regions", ())
    )


def present(value, quote):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return any(
            float(n.replace(",", "")) == float(value)
            for n in re.findall(r"(?<![\w.])[+-]?\d[\d,]*(?:\.\d+)?(?![\w.])", quote)
        )
    raw = collapsed(value)
    options = ALIASES.get(raw, (raw,))
    return any(
        re.search(r"(?<![\w.])" + re.escape(v) + r"(?![\w.])", collapsed(quote))
        for v in options
        if v
    )


def validate_fields(item, *, fulltext, locators, parse_locator):
    sources = item.get("field_sources") or {}
    result = {}
    for field in FIELDS:
        value = item.get(field)
        source = sources.get(field, {}) if isinstance(sources, dict) else {}
        source = source if isinstance(source, dict) else {}
        quote = str(source.get("quote") or item.get("verbatim_span") or "")
        marker = source.get("source_marker") or item.get("source_marker")
        located = bool(quote) and region_contains(fulltext, marker, quote, parse_locator, locators)
        matched = value is not None and present(value, quote)
        # Explicit cell coordinates bind a table number to its metric column and model row.
        # Coordinates are checked against the actual quoted table, not model-produced headers.
        if source.get("row") is not None or source.get("column") is not None:
            lines = [line for line in quote.splitlines() if line.strip()]
            cells = [[c.strip() for c in line.strip().strip("|").split("|")] for line in lines]
            try:
                row, col = int(source["row"]), int(source["column"])
                matched = (
                    (row > 0 or (row == 0 and field in {"metric_name", "unit"}))
                    and col >= 0
                    and len(cells[0]) == len(cells[row])
                    and present(value, cells[row][col])
                )
                if field == "value":
                    matched = (
                        matched
                        and present(item.get("metric_name"), cells[0][col])
                        and present(item.get("model_family"), cells[row][0])
                    )
            except (ValueError, TypeError, KeyError, IndexError):
                matched = False
        elif field in {"value", "metric_name", "model_family"} and ("|" in quote or "\t" in quote):
            matched = False  # Flattened multi-column text has no trustworthy cell alignment.
        elif field == "value":
            # The metric must immediately govern the chosen value; a different measurement
            # elsewhere in the same sentence is not sufficient.
            governing = False
            aliases = ALIASES.get(
                collapsed(item.get("metric_name")), (collapsed(item.get("metric_name")),)
            )
            for number in re.finditer(r"(?<![\w.])[+-]?\d[\d,]*(?:\.\d+)?(?![\w.])", quote):
                if not present(value, number.group()):
                    continue
                prefix = collapsed(quote[max(0, number.start() - 60) : number.start()])
                governing |= any(
                    re.search(re.escape(a) + r"[^\d.;。]{0,35}$", prefix) for a in aliases if a
                )
            matched = matched and governing
        if field in {"dataset", "split", "model_family", "unit"} and "|" not in quote:
            # A dimension appearing in an unrelated sentence is not this result's condition.
            sentences = re.split(r"(?<=[。;!?])\s*|(?<=\.)\s+(?=[A-Z])", quote)
            matched = matched and any(
                present(value, sentence)
                and present(item.get("value"), sentence)
                and present(item.get("metric_name"), sentence)
                for sentence in sentences
            )
        result[field] = {
            "value": value,
            "quote": quote,
            "source_marker": marker,
            "status": "verified" if located and matched else "unknown",
        }
    return {"version": VERSION, "fields": result}


def comparison_verified(provenance):
    return bool(
        provenance
        and provenance.get("version") == VERSION
        and all(
            provenance.get("fields", {}).get(f, {}).get("status") == "verified"
            for f in REQUIRED_COMPARISON
        )
    )


def derived_comparisons(evidence):
    """Arithmetic context for reviewers; no significance or unknown metric direction inferred."""
    from decimal import Decimal, InvalidOperation
    from itertools import combinations

    groups = {}
    for unit in evidence:
        for measurement in unit.get("measurements") or []:
            if comparison_verified(measurement.get("provenance_json")) and measurement.get(
                "comparability_key"
            ):
                groups.setdefault(measurement["comparability_key"], []).append((unit, measurement))
    result = []
    higher = {"accuracy", "acc", "f1", "auc", "auroc", "precision", "recall"}
    lower = {"mse", "rmse", "mae", "error_rate", "latency", "wer"}
    for pairs in groups.values():
        for (a, x), (b, y) in combinations(pairs, 2):
            if any(x.get(f) != y.get(f) for f in ("metric_name", "dataset", "split", "unit")):
                continue
            try:
                left, right = Decimal(str(x["value"])), Decimal(str(y["value"]))
            except (InvalidOperation, KeyError):
                continue
            if not left.is_finite() or not right.is_finite():
                continue
            metric = str(x.get("metric_name", "")).casefold()
            direction = "higher" if metric in higher else "lower" if metric in lower else "unknown"
            result.append(
                {
                    "evidence_ids": [
                        str(a.get("evidence_id") or a.get("id")),
                        str(b.get("evidence_id") or b.get("id")),
                    ],
                    "models": [x.get("model_family"), y.get("model_family")],
                    "metric": metric,
                    "dataset": x.get("dataset"),
                    "split": x.get("split"),
                    "unit": x.get("unit"),
                    "values": [str(left), str(right)],
                    "difference_left_minus_right": str(left - right),
                    "preferred_direction": direction,
                    "better_model": None
                    if direction == "unknown" or left == right
                    else x.get("model_family")
                    if ((left > right) == (direction == "higher"))
                    else y.get("model_family"),
                    "significance": "not_assessed",
                }
            )
            if len(result) >= 64:
                return result
    return result
