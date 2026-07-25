"""NUMLINT：正文数值 vs 素材解析值一致性检查（设计 §4.4.2）。

产品红线：**系统在任何路径下都不生成虚构实验数值**。防线有两层——
prompt 层（写作器明令禁止编造）与本模块的 lint 层。这里是后者：
扫描正文中的数值，凡是不能在 `user_asset.parsed_json` 中找到出处的，
一律标记为 `unsourced`，由前端显式呈现、导出前强制展示 TODO 清单。

不阻断（draft-first）：lint 只产出标记，不拒绝交付；但标记必须真实、不可静默。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ingest.assets import extract_numbers, normalize_number

# 这些数值不算「实验数据」：年份、章节/图表序号、百分之百之类的修辞。
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
TRIVIAL_NUMBERS = frozenset({"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "100"})
# 序数上下文：图 1 / 表 2 / 第 3 节 / Figure 1 / Table 2 / Section 3 / Eq. 4
_ORDINAL_CONTEXT_RE = re.compile(
    r"(?:图|表|式|第|章节|节)\s*$|(?:figure|fig\.|table|tab\.|section|sec\.|equation|eq\.)\s*$",
    re.IGNORECASE,
)


@dataclass
class NumberFinding:
    value: str
    section_key: str
    context: str
    status: str  # sourced | unsourced | ignored
    source_asset: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "section_key": self.section_key,
            "context": self.context,
            "status": self.status,
            "source_asset": self.source_asset,
        }


@dataclass
class NumLintReport:
    findings: list[NumberFinding] = field(default_factory=list)
    asset_number_count: int = 0

    @property
    def unsourced(self) -> list[NumberFinding]:
        return [f for f in self.findings if f.status == "unsourced"]

    @property
    def sourced(self) -> list[NumberFinding]:
        return [f for f in self.findings if f.status == "sourced"]

    @property
    def consistent(self) -> bool:
        """正文数字与素材 100% 一致（无未溯源数值）。"""
        return not self.unsourced

    def to_payload(self) -> dict[str, Any]:
        return {
            "consistent": self.consistent,
            "asset_number_count": self.asset_number_count,
            "checked_count": len(self.findings),
            "sourced_count": len(self.sourced),
            "unsourced_count": len(self.unsourced),
            "unsourced": [f.to_payload() for f in self.unsourced],
        }


def build_asset_index(parsed_assets: list[dict[str, Any]]) -> dict[str, str]:
    """数值 → 素材来源标识。素材解析结果是正文数字的唯一合法出处。"""
    index: dict[str, str] = {}
    for asset in parsed_assets:
        if not isinstance(asset, dict):
            continue
        label = str(asset.get("filename") or asset.get("type") or "asset")
        for number in asset.get("numbers") or []:
            index.setdefault(normalize_number(str(number)), label)
        for key, value in (asset.get("numeric_cells") or {}).items():
            index.setdefault(normalize_number(str(value)), f"{label}[{key}]")
    return index


def lint_text(
    text: str,
    *,
    section_key: str,
    asset_index: dict[str, str],
) -> list[NumberFinding]:
    """扫描一段正文里的数值。"""
    findings: list[NumberFinding] = []
    for match in re.finditer(
        r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?%?", text or ""
    ):
        raw = match.group(0)
        value = normalize_number(raw)
        context = text[max(0, match.start() - 40) : match.end() + 40]
        if _is_ignorable(value, text, match.start()):
            findings.append(
                NumberFinding(
                    value=value,
                    section_key=section_key,
                    context=context,
                    status="ignored",
                )
            )
            continue
        source = asset_index.get(value)
        findings.append(
            NumberFinding(
                value=value,
                section_key=section_key,
                context=context,
                status="sourced" if source else "unsourced",
                source_asset=source,
            )
        )
    return findings


def lint_sections(
    sections: list[dict[str, Any]],
    *,
    parsed_assets: list[dict[str, Any]],
) -> NumLintReport:
    """对整篇文稿做数字一致性检查。

    ``sections`` 形如 ``[{"section_key": "s1", "text": "..."}]``。
    没有任何素材时，正文里**任何**实验数值都算未溯源——这正是纯生成模式
    必须使用 `\\todo{待补充实验数据}` 占位而不是写数字的原因。
    """
    index = build_asset_index(parsed_assets)
    report = NumLintReport(asset_number_count=len(index))
    for section in sections:
        report.findings.extend(
            lint_text(
                str(section.get("text") or ""),
                section_key=str(section.get("section_key") or ""),
                asset_index=index,
            )
        )
    return report


def _is_ignorable(value: str, text: str, start: int) -> bool:
    if YEAR_RE.match(value):
        return True
    if value in TRIVIAL_NUMBERS:
        return True
    prefix = text[max(0, start - 12) : start]
    return bool(_ORDINAL_CONTEXT_RE.search(prefix))


def numbers_in(text: str) -> list[str]:
    return extract_numbers(text)


__all__ = [
    "NumLintReport",
    "NumberFinding",
    "build_asset_index",
    "lint_sections",
    "lint_text",
    "numbers_in",
]
