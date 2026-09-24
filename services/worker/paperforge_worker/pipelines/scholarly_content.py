"""Source-bound mathematical content and balanced, question-local evidence packets."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from typing import Any

from paper_ir.mathematics import source_formulas
from paper_ir.schema import CiteRun, EquationBlock, GroundingRun, MathInlineRun, TextRun, XRefRun

DEPTH_VERSION = "scholarly-content-v1"
_ROLES = {
    "mechanism": r"method|mechanism|algorithm|architecture|方法|机制|算法|架构",
    "definitions": r"defin|denot|where|represents|定义|表示|其中|变量",
    "assumptions": r"assum|constraint|subject to|provided|假设|约束|条件",
    "experimental_setup": r"dataset|split|protocol|train|setting|数据集|划分|训练|设置",
    "baselines": r"baseline|compared with|基线|对照",
    "ablations": r"ablation|消融",
    "limitations": r"limitation|limited|failure|however|局限|限制|失效|然而",
}


def enrich_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """Each field is an exact source passage, not a model-invented interpretation."""
    if row.get("grade") == "D_abstract_only":
        return {**row, "depth": {}, "formulas": []}
    text = str(row.get("text") or "")
    passages = [s.strip() for s in re.split(r"(?<=[。！？.!?])\s+|\n\s*\n", text) if s.strip()]
    depth = {
        role: [s for s in passages if re.search(pattern, s, re.I)][:3]
        for role, pattern in _ROLES.items()
    }
    return {
        **row,
        "depth": depth,
        "formulas": source_formulas(text, object_ref=str(row.get("object_ref") or "")),
    }


def balanced_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin across roles and works so result cells cannot crowd out mechanisms."""
    buckets: dict[tuple[str, str], deque] = defaultdict(deque)
    for raw in rows:
        row = enrich_evidence(raw)
        role = (
            "formula"
            if row["formulas"]
            else next((k for k, v in row["depth"].items() if v), "result")
        )
        buckets[(role, str(row.get("work_id") or row.get("cite_key")))].append(row)
    result = []
    while any(buckets.values()):
        for name in sorted(buckets, key=lambda k: (k[0] != "formula", k[0] == "result", k)):
            bucket = buckets[name]
            if bucket:
                result.append(bucket.popleft())
    return result


def formula_catalog(
    evidence: list[dict[str, Any]], assets: list[dict[str, Any]]
) -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    sources = [enrich_evidence(row) for row in evidence if row.get("grade") != "D_abstract_only"]
    for asset in assets:
        ref = str(asset.get("_asset_ref") or "")
        if ref:
            # Traverse string values rather than JSON-escaped LaTeX.
            def strings(value):
                if isinstance(value, str):
                    yield value
                elif isinstance(value, dict):
                    for child in value.values():
                        yield from strings(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from strings(child)

            text = "\n".join(strings(asset))
            sources.append({"source_ref": ref, "formulas": source_formulas(text)})
    budget = 12000
    for row in sources:
        for formula in row.get("formulas") or []:
            identity = str(row.get("evidence_id") or row.get("source_ref")) + formula["latex"]
            key = "m" + hashlib.sha256(identity.encode()).hexdigest()[:12]
            neighbors = [
                other
                for other in sources
                if row.get("work_id")
                and other.get("work_id") == row.get("work_id")
                and other.get("section_path") == row.get("section_path")
                and any(other.get("depth", {}).get(role) for role in ("definitions", "assumptions"))
            ]
            context = (
                formula["context"]
                + "\n"
                + "\n".join(str(other.get("text") or "") for other in neighbors[:2])
            )
            candidate = {
                **formula,
                "context": context,
                "source_id": key,
                "cite_keys": [row["cite_key"]] if row.get("cite_key") else [],
                "evidence_ids": [row["evidence_id"]] if row.get("evidence_id") else [],
                "source_refs": [row["source_ref"]] if row.get("source_ref") else [],
                "locator": {k: row.get(k) for k in ("page", "section_path", "object_ref")},
            }
            candidate["evidence_ids"] = list(
                dict.fromkeys(
                    candidate["evidence_ids"]
                    + [other["evidence_id"] for other in neighbors[:2] if other.get("evidence_id")]
                )
            )
            cost = len(json.dumps(candidate, ensure_ascii=False)) + len(key) + 6
            if cost <= budget:
                catalog[key] = candidate
                budget -= cost
    return catalog


MATH_INSTRUCTION = """
Mathematical content contract (optional; never add formulas merely to meet a quota):
Describe a mechanism or assumption only when an explicit source passage supports it.
Do not infer mechanisms from result cells, titles or general knowledge.
Do not generalize one study's method to a whole field.
When a formula defines this section's central method or objective, include and explain it.
Only use the supplied SOURCE_FORMULAS. Select a source_id; never invent or alter its LaTeX.
For a displayed formula add a top-level "equations" list with objects:
{"source_id":"supplied id", "after_paragraph":0,
 "explanation":"explain symbols and assumptions using the source context"}.
after_paragraph is the zero-based index of its introducing paragraph.
Explain its role in the argument in the paper language.
For inline math put {{math:source_id}} in sentence text.
Bind that sentence to the source evidence/citation or asset.
To reference a selected displayed formula put {{eq:source_id}} in sentence text.
Do not use raw $...$ in prose. Do not put formulas in an abstract.
Missing formulas are evidence gaps, not permission to derive a new one.
"""
_TOKEN = re.compile(r"\{\{(math|eq):([A-Za-z0-9_-]+)\}\}")


def math_tokens(text: str) -> list[tuple[str, str]]:
    return _TOKEN.findall(text)


def math_runs(text: str, catalog: dict[str, dict], labels: dict[str, str]) -> list[Any]:
    runs: list[Any] = []
    cursor = 0
    for match in _TOKEN.finditer(text):
        if match.start() > cursor:
            runs.append(TextRun(v=text[cursor : match.start()]))
        kind, key = match.groups()
        item = catalog[key]
        if kind == "eq":
            runs.append(XRefRun(target=labels[key], kind="equation"))
        else:
            runs.append(MathInlineRun(v=item["latex"]))
        # Bind math independently of the surrounding prose's citations.
        if item.get("cite_keys"):
            runs.append(CiteRun(keys=item["cite_keys"], evidence_ids=item["evidence_ids"]))
        if item.get("source_refs"):
            runs.append(GroundingRun(source_refs=item["source_refs"]))
        cursor = match.end()
    if cursor < len(text):
        runs.append(TextRun(v=text[cursor:]))
    return runs


def accept_math(draft, raw: dict, catalog: dict[str, dict]) -> None:
    accepted = []
    issues = []
    seen = set()
    for item in (raw.get("equations") or []) if isinstance(raw.get("equations"), list) else []:
        if not isinstance(item, dict):
            continue
        key = item.get("source_id")
        position = item.get("after_paragraph")
        explanation = str(item.get("explanation") or "").strip()
        if (
            not isinstance(key, str)
            or key not in catalog
            or type(position) is not int
            or not 0 <= position < len(draft.paragraphs)
            or not explanation
        ):
            issues.append({"code": "formula_source_or_explanation_missing", "source_id": key})
            continue
        if key in seen:
            continue
        seen.add(key)
        accepted.append(
            {
                **catalog[key],
                "after_paragraph": position,
                "explanation": explanation,
                "label": f"eq:{draft.section_key}:{key}",
            }
        )
    draft.equations = accepted
    draft.math_catalog = catalog
    for paragraph in draft.paragraphs:
        if not paragraph.get("sentences") and math_tokens(paragraph.get("text", "")):
            paragraph["sentences"] = [
                {
                    "text": paragraph["text"],
                    "cite_keys": paragraph.get("cite_keys", []),
                    "evidence_ids": [],
                    "source_refs": [],
                }
            ]
        for sentence in paragraph.get("sentences") or []:
            tokens = math_tokens(sentence.get("text", ""))
            for _, key in tokens:
                if key in catalog:
                    for field in ("cite_keys", "evidence_ids", "source_refs"):
                        sentence[field] = list(
                            dict.fromkeys(sentence.get(field, []) + catalog[key][field])
                        )
            if any(
                key not in catalog or (kind == "eq" and key not in seen) for kind, key in tokens
            ):
                sentence["text"] = ""
                issues.append({"code": "formula_reference_invalid"})
        if paragraph.get("sentences"):
            paragraph["text"] = " ".join(s["text"] for s in paragraph["sentences"] if s["text"])
    draft.generation["scholarly_content_version"] = DEPTH_VERSION
    if not catalog and re.search(
        r"objective function|loss function|formal definition|"
        r"损失函数|目标函数|数学定义|数学建模",
        draft.title,
        re.I,
    ):
        issues.append({"code": "formula_evidence_missing"})
    draft.generation["formula_issues"] = issues


def equation_block(item: dict) -> EquationBlock:
    return EquationBlock(
        latex=item["latex"],
        label=item["label"],
        source_ids=item.get("evidence_ids", []) + item.get("source_refs", []),
        source_latex=item["latex"],
        explanation=item["explanation"],
        source_context=item.get("context", ""),
    )


def thematic_tables(section: dict, evidence: list[dict], *, language: str) -> list[dict]:
    """Only compare question-aligned, sourced dimensions; never dump a library matrix."""
    zh = language == "zh"
    purpose = " ".join(
        str(section.get(k) or "") for k in ("title", "summary", "argument_points", "synthesis")
    )
    if not section.get("comparison_clusters") and not re.search(
        r"compar|contrast|difference|比较|对比|差异|异同", purpose, re.I
    ):
        return []
    usable = [
        enrich_evidence(r)
        for r in evidence
        if r.get("cite_key")
        and r.get("evidence_id")
        and r.get("grade") in {"A_located_structured", "B_located_prose"}
    ]
    groups: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for row in usable:
        for m in row.get("measurements") or []:
            if (
                m.get("comparability_key")
                and m.get("dataset")
                and m.get("metric_name")
                and m.get("value") is not None
            ):
                groups[str(m["comparability_key"])].append((row, m))
    tables = []
    for key, pairs in groups.items():
        # Same-study multiple settings are not collapsed into a cherry-picked best result.
        by_work: dict[str, list] = defaultdict(list)
        for row, m in pairs:
            by_work[str(row.get("work_id") or row["cite_key"])].append((row, m))
        if not 2 <= len(by_work) <= 8:
            continue
        selected = []
        for entries in by_work.values():
            distinct = {
                (str(m["value"]), str(m.get("unit")), str(m.get("split"))) for _, m in entries
            }
            if len(distinct) != 1:
                break
            selected.append(entries[0])
        if len(selected) != len(by_work):
            continue
        if (
            len(
                {
                    (m["dataset"], m["metric_name"], m.get("unit"), m.get("split"))
                    for _, m in selected
                }
            )
            != 1
        ):
            continue
        tables.append(
            {
                "headers": ["研究", "数据集", "指标", "结果"]
                if zh
                else ["Study", "Dataset", "Metric", "Result"],
                "rows": [
                    [
                        str(r.get("title") or r["cite_key"])[:100],
                        m["dataset"],
                        m["metric_name"],
                        f"{m['value']}{m.get('unit') or ''}",
                    ]
                    for r, m in selected
                ],
                "cite_keys": list(dict.fromkeys(r["cite_key"] for r, _ in selected)),
                "evidence_ids": [r["evidence_id"] for r, _ in selected],
                "comparison_key": key,
                "caption": (
                    "相同实验条件下的结果比较：" if zh else "Results under shared conditions: "
                )
                + str(section.get("title") or ""),
            }
        )
    if not tables:
        studies: dict[str, dict[str, Any]] = {}
        for row in usable:
            study = studies.setdefault(
                str(row.get("work_id") or row["cite_key"]),
                {"rows": [], "mechanism": [], "assumptions": []},
            )
            study["rows"].append(row)
            for role in ("mechanism", "assumptions"):
                study[role].extend(row["depth"].get(role) or [])
        complete = [
            s
            for s in studies.values()
            if s["mechanism"] and s["assumptions"] and s["mechanism"][0] != s["assumptions"][0]
        ]
        if 2 <= len(complete) <= 8:
            sources = [r for s in complete for r in s["rows"]]
            tables.append(
                {
                    "headers": ["研究", "方法机制", "假设与约束"]
                    if zh
                    else ["Study", "Mechanism", "Assumptions"],
                    "rows": [
                        [
                            str(s["rows"][0].get("title") or s["rows"][0]["cite_key"])[:100],
                            s["mechanism"][0][:180],
                            s["assumptions"][0][:180],
                        ]
                        for s in complete
                    ],
                    "cite_keys": list(dict.fromkeys(r["cite_key"] for r in sources)),
                    "evidence_ids": list(dict.fromkeys(r["evidence_id"] for r in sources)),
                    "caption": ("方法与适用条件比较：" if zh else "Methods and assumptions: ")
                    + str(section.get("title") or ""),
                }
            )
    # A chapter's main argument should remain prose. Further groups are in the attachment.
    for index, table in enumerate(tables[:2]):
        table.update(
            label=f"tab:{section.get('key')}:comparison-{index + 1}",
            purpose=section.get("summary") or section.get("title"),
            automatic=True,
        )
    return tables[:2]


def math_quality_issues(rows: list[Any]) -> list[dict[str, Any]]:
    from paper_ir.mathematics import valid_math

    issues = []
    labels = {
        b.get("label"): b.get("type")
        for row in rows
        for b in (getattr(row, "body_ir_json", None) or {}).get("blocks", [])
        if b.get("label")
    }
    for row in rows:
        key = row.section_key
        generation = getattr(row, "generation_json", None) or {}
        for item in generation.get("formula_issues") or []:
            issues.append(
                {**item, "section_key": key, "message": "公式来源、解释或正文位置需要补充核对"}
            )
        blocks = (getattr(row, "body_ir_json", None) or {}).get("blocks", [])
        for block in blocks:
            if block.get("type") == "equation":
                if not valid_math(block.get("latex", "")):
                    issues.append(
                        {
                            "code": "formula_invalid",
                            "section_key": key,
                            "message": "公式表达式无效或含不支持的命令",
                        }
                    )
                if not block.get("source_ids") or not block.get("explanation"):
                    issues.append(
                        {
                            "code": "formula_provenance_missing",
                            "section_key": key,
                            "message": "公式缺少来源绑定或符号与适用条件解释",
                        }
                    )
                if block.get("source_latex") and block["latex"] != block["source_latex"]:
                    issues.append(
                        {
                            "code": "formula_source_changed",
                            "section_key": key,
                            "message": "公式与来源表达式不一致，需要核对",
                        }
                    )
            for run in block.get("runs") or []:
                if run.get("t") == "xref" and run.get("kind") == "equation":
                    if labels.get(run.get("target")) != "equation":
                        issues.append(
                            {
                                "code": "formula_reference_invalid",
                                "section_key": key,
                                "message": "公式引用没有对应的公式",
                            }
                        )
                if run.get("t") == "math_inline" and not valid_math(run.get("v", "")):
                    issues.append(
                        {
                            "code": "formula_invalid",
                            "section_key": key,
                            "message": "行内公式表达式无效",
                        }
                    )
    return issues
