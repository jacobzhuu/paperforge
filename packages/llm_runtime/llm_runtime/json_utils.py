from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

# 迁移自 DeepSearch literature_review/json_utils.py。
# clean_and_parse_json 原样保留；DeepSearch 的 finding-id 过滤（溯源专属）替换为
# PaperForge R2 的 cite_keys 白名单过滤（见 docs/design.md §4.4.3）。


def clean_and_parse_json(text: str) -> Any:
    """
    Cleans an LLM response string by stripping markdown code block tags,
    finding the outermost JSON structure (object or array), and parsing it.
    """
    cleaned = text.strip()

    # 1. Strip markdown code block wrappers
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if match:
        candidate = match.group(1).strip()
    else:
        candidate = cleaned

    # 2. Extract outermost JSON structure if there is leading/trailing explanation text
    first_brace = candidate.find("{")
    first_bracket = candidate.find("[")

    start_idx = -1
    end_char = ""

    if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
        start_idx = first_brace
        end_char = "}"
    elif first_bracket != -1:
        start_idx = first_bracket
        end_char = "]"

    if start_idx != -1:
        end_idx = candidate.rfind(end_char)
        if end_idx != -1 and end_idx > start_idx:
            candidate = candidate[start_idx : end_idx + 1]

    # 3. Parse JSON
    return json.loads(candidate)


# R2 写作约束：段落 cite_keys 必须 ⊆ 项目白名单。任何以下键名的字符串列表
# 都会被裁剪到白名单内，杜绝幻觉引用进入 IR。
_CITE_KEY_LIST_KEYS = frozenset({"cite_keys", "citekeys", "citations"})


@dataclass(frozen=True)
class CiteKeyViolation:
    """One R2 whitelist violation found in structured LLM output."""

    path: str
    rejected_keys: tuple[str, ...]


def purify_llm_json(
    raw_text: str,
    *,
    allowed_cite_keys: set[str] | None = None,
    mode: Literal["report", "strip"] = "report",
) -> tuple[Any, list[CiteKeyViolation]]:
    """
    Parse structured output and report R2 cite-key violations.

    ``report`` preserves the original value so the caller can ask the model to
    rewrite once. ``strip`` removes rejected keys for the second-pass fallback
    while still returning violations for the editor marker.
    """
    if mode not in {"report", "strip"}:
        raise ValueError(f"unsupported cite-key purification mode: {mode}")
    parsed = clean_and_parse_json(raw_text)
    violations: list[CiteKeyViolation] = []
    if allowed_cite_keys is not None:
        _audit_cite_keys(
            parsed,
            allowed_cite_keys,
            violations,
            path="",
            strip=mode == "strip",
        )
    return parsed, violations


def _audit_cite_keys(
    data: Any,
    allowed: set[str],
    violations: list[CiteKeyViolation],
    *,
    path: str,
    strip: bool,
) -> None:
    if isinstance(data, dict):
        for key, value in list(data.items()):
            child_path = f"{path}.{key}" if path else key
            is_paper_ir_cite_keys = key == "keys" and data.get("t") == "cite"
            if (
                (key in _CITE_KEY_LIST_KEYS or is_paper_ir_cite_keys)
                and isinstance(value, list)
            ):
                normalized = [str(item) for item in value]
                rejected = tuple(item for item in normalized if item not in allowed)
                if rejected:
                    violations.append(
                        CiteKeyViolation(path=child_path, rejected_keys=rejected)
                    )
                if strip:
                    data[key] = [item for item in normalized if item in allowed]
            else:
                _audit_cite_keys(
                    value,
                    allowed,
                    violations,
                    path=child_path,
                    strip=strip,
                )
    elif isinstance(data, list):
        for index, item in enumerate(data):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            _audit_cite_keys(
                item,
                allowed,
                violations,
                path=child_path,
                strip=strip,
            )
