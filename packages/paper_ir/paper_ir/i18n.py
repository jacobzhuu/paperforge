"""Small PaperForge-owned translation catalog.

The legacy PRISMA, screening, evidence-matrix, conservation-ledger, and formal
synthesis blocking strings are intentionally excluded: those machines are not
part of PaperForge's draft-first product model.
"""

from __future__ import annotations

from typing import Any

ReportLanguage = str  # "zh" | "en"

_EN: dict[str, str] = {
    "md.abstract": "Abstract",
    "md.background": "Background",
    "md.methods": "Methods",
    "md.results": "Results",
    "md.conclusion": "Conclusion",
    "md.information_sources": "Information sources",
    "md.search_strategy": "Search strategy",
    "md.limitations": "Limitations",
    "md.references": "References",
    "section.introduction": "Introduction",
    "section.historical_timeline": "Historical Timeline and Milestones",
    "section.method_evolution": "Method Evolution",
    "section.controversies_limitations": "Controversies and Limitations",
    "section.future_directions": "Future Directions and Coverage Gaps",
    "editor.citation_removed": "Citation removed because its key is not in the project whitelist.",
    "todo.experimental_data": "Experimental data to be supplied",
    "lang_name.en": "English",
    "lang_name.zh": "Chinese",
}

_ZH: dict[str, str] = {
    "md.abstract": "摘要",
    "md.background": "背景",
    "md.methods": "方法",
    "md.results": "结果",
    "md.conclusion": "结论",
    "md.information_sources": "信息来源",
    "md.search_strategy": "检索策略",
    "md.limitations": "局限性",
    "md.references": "参考文献",
    "section.introduction": "引言",
    "section.historical_timeline": "历史时间线与里程碑",
    "section.method_evolution": "方法演进",
    "section.controversies_limitations": "争议与局限",
    "section.future_directions": "未来方向与覆盖缺口",
    "editor.citation_removed": "引用键不在项目白名单中，引用已移除。",
    "todo.experimental_data": "待补充实验数据",
    "lang_name.en": "英文",
    "lang_name.zh": "中文",
}


def normalize_lr_report_language(value: Any) -> ReportLanguage:
    """Normalize free-form language tags to ``zh`` or ``en`` (default ``en``)."""
    text = str(value or "").strip().casefold().replace("_", "-")
    if text.startswith("zh") or text in {"cn", "chinese", "中文"}:
        return "zh"
    return "en"


def resolve_report_language(scope: dict[str, Any] | None) -> ReportLanguage:
    """Resolve the output language from a PaperForge project scope."""
    scope = scope if isinstance(scope, dict) else {}
    explicit = scope.get("report_language")
    if explicit:
        return normalize_lr_report_language(explicit)
    languages = scope.get("languages") if isinstance(scope.get("languages"), list) else []
    if languages:
        return normalize_lr_report_language(languages[0])
    return "en"


def t(key: str, lang: ReportLanguage = "en", **kwargs: Any) -> str:
    """Look up a localized string; missing keys fall back to English."""
    table = _ZH if lang == "zh" else _EN
    template = table.get(key) or _EN.get(key) or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError):
            return template
    return template


def language_display_name(lang: ReportLanguage) -> str:
    return t(f"lang_name.{lang}", lang)
