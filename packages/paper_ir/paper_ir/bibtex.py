from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256

from paper_ir.reference import ReferenceMetadata

# R3 参考文献确定性生成（方案 §4.4.3）。
# BibTeX / cite-key 由代码从库内元数据确定性生成，LLM 在任何环节都不书写、不修改参考文献条目。
# 使用 pybtex 的数据库模型序列化，保证 .bib 输出稳定、可编译。

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "with",
        "using", "via", "toward", "towards", "based", "study", "analysis",
    }
)


def _ascii_token(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "", value).lower()


def _stable_fallback(ref: ReferenceMetadata) -> str:
    source = "|".join(
        (
            ref.work_key,
            ref.title or ref.normalized_title or "",
            str(ref.publication_year or ""),
        )
    )
    return sha256(source.encode("utf-8")).hexdigest()[:8]


def _looks_like_initials(token: str) -> bool:
    """判断是否为缩写名（``Q``、``J.R.``、``QW``）。

    Europe PMC 的 authorString 用「姓 + 缩写名」（``Zhang Q``），若一律取最后一段
    会把 cite key 写成 ``q2025...``。缩写名一律很短且不含小写字母。
    """
    stripped = token.replace(".", "").strip()
    if not stripped or len(stripped) > 3:
        return False
    return stripped.isupper() or not stripped.isalpha()


def _first_author_surname(ref: ReferenceMetadata) -> str:
    authors = ref.authors
    if authors:
        first = min(authors, key=lambda a: a.get("author_order", 0) if isinstance(a, dict) else 0)
        name = str(first.get("author_name") or "").strip() if isinstance(first, dict) else ""
        if name:
            if "," in name:
                # BibTeX 惯例 ``Last, First``。
                surname = name.split(",")[0].strip()
            else:
                parts = name.split()
                # ``Zhang Q`` 取首段；``Ada Lovelace`` 取末段。
                surname = (
                    parts[0]
                    if len(parts) > 1 and _looks_like_initials(parts[-1])
                    else parts[-1]
                )
            token = _ascii_token(surname)
            if token:
                return token
    return "anon"


def _title_keyword(ref: ReferenceMetadata) -> str:
    title = (ref.title or ref.normalized_title or "").strip()
    for raw in re.split(r"[^0-9a-zA-Z]+", title):
        token = raw.strip().lower()
        if token and token not in _STOPWORDS and len(token) > 2:
            return token
    return "work"


def make_bibtex_key(ref: ReferenceMetadata, *, taken: set[str] | None = None) -> str:
    """Assign a safe deterministic key at verified-library insertion time only."""
    year = str(ref.publication_year) if ref.publication_year else "nd"
    base = f"{_first_author_surname(ref)}{year}{_title_keyword(ref)}"
    base = re.sub(r"[^0-9a-zA-Z]+", "", base)
    if base in {"", "anonndwork"} or (
        base.startswith("anon") and _title_keyword(ref) == "work"
    ):
        base = f"ref{year}{_stable_fallback(ref)}"
    if taken is None:
        return base
    if base not in taken:
        taken.add(base)
        return base
    for i in range(ord("a"), ord("z") + 1):
        candidate = f"{base}{chr(i)}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    # 极端冲突：数字后缀兜底。
    n = 1
    while f"{base}{n}" in taken:
        n += 1
    key = f"{base}{n}"
    taken.add(key)
    return key


def _entry_type(ref: ReferenceMetadata) -> str:
    wt = (ref.work_type or "").lower()
    if ref.arxiv_id or "preprint" in wt:
        return "misc" if not ref.venue_name else "article"
    if "book" in wt:
        return "book"
    if "conference" in wt or "proceedings" in wt or "inproceedings" in wt:
        return "inproceedings"
    return "article"


def _authors_bibtex(ref: ReferenceMetadata) -> str:
    names = []
    for a in sorted(
        ref.authors,
        key=lambda a: a.get("author_order", 0) if isinstance(a, dict) else 0,
    ):
        if isinstance(a, dict):
            name = str(a.get("author_name") or "").strip()
            if name:
                names.append(name)
    return " and ".join(names)


def build_bibtex_database(refs: Iterable[ReferenceMetadata]):
    """用 pybtex 构建 BibliographyData（延迟导入，便于无 pybtex 时给出清晰错误）。"""
    try:
        from pybtex.database import BibliographyData, Entry
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的显式提示
        raise RuntimeError(
            "R3 参考文献生成需要 pybtex：请安装 `pybtex`（见 pyproject 依赖）。"
        ) from exc

    taken: set[str] = set()
    entries: list[tuple[str, Entry]] = []
    for ref in refs:
        key = (ref.bibtex_key or "").strip()
        if not key:
            raise ValueError(
                f"reference {ref.work_key!r} has no persisted bibtex_key; "
                "assign it when the verified library entry is created"
            )
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z:._+-]*", key):
            raise ValueError(f"reference {ref.work_key!r} has unsafe bibtex_key {key!r}")
        if key in taken:
            raise ValueError(f"duplicate persisted bibtex_key: {key}")
        taken.add(key)
        fields: dict[str, str] = {}
        title = (ref.title or ref.normalized_title or "").strip()
        if title:
            fields["title"] = title
        if ref.venue_name:
            fields["journal" if _entry_type(ref) == "article" else "booktitle"] = ref.venue_name
        if ref.publication_year:
            fields["year"] = str(ref.publication_year)
        if ref.publisher:
            fields["publisher"] = ref.publisher
        if ref.doi:
            fields["doi"] = ref.doi
        if ref.arxiv_id:
            fields["eprint"] = ref.arxiv_id
            fields["archivePrefix"] = "arXiv"
        entry = Entry(_entry_type(ref), fields=fields)
        authors = _authors_bibtex(ref)
        if authors:
            entry.fields["author"] = authors
        entries.append((key, entry))
    return BibliographyData(entries=dict(entries))


def render_bibtex(refs: Iterable[ReferenceMetadata]) -> str:
    """从库内元数据确定性渲染 .bib 文本（R3）。"""
    return build_bibtex_database(refs).to_string("bibtex")


@dataclass(frozen=True)
class BibtexImportEntry:
    """从用户 .bib 解析出的**引用线索**，尚未核验。

    R1：解析结果绝不直接入库——必须先经 scholar_gateway.verify 反查，
    以真实 provider 响应为准重建元数据（用户 .bib 里的字段可能有误或杜撰）。
    """

    key: str | None
    entry_type: str | None
    title: str | None
    authors: tuple[str, ...]
    publication_year: int | None
    doi: str | None
    arxiv_id: str | None
    venue_name: str | None


def parse_bibtex_entries(text: str) -> list[BibtexImportEntry]:
    """解析 .bib 文本为待核验线索列表。无法解析的条目直接跳过。"""
    try:
        from pybtex.database import parse_string
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的显式提示
        raise RuntimeError("BibTeX 导入需要 pybtex：请安装 `pybtex`。") from exc

    try:
        database = parse_string(text, "bibtex")
    except Exception as exc:  # noqa: BLE001 - pybtex 抛多种解析异常
        raise ValueError(f"invalid bibtex input: {type(exc).__name__}") from exc

    entries: list[BibtexImportEntry] = []
    for key, entry in database.entries.items():
        fields = {k.lower(): str(v).strip() for k, v in entry.fields.items()}
        authors: list[str] = []
        for person in entry.persons.get("author", []):
            name = " ".join(
                part
                for part in (
                    " ".join(person.first_names),
                    " ".join(person.middle_names),
                    " ".join(person.last_names),
                )
                if part
            ).strip()
            if name:
                authors.append(name)
        year = fields.get("year", "")
        entries.append(
            BibtexImportEntry(
                key=str(key) if key else None,
                entry_type=entry.type,
                title=_strip_braces(fields.get("title")),
                authors=tuple(authors),
                publication_year=int(year) if year[:4].isdigit() else None,
                doi=fields.get("doi") or None,
                arxiv_id=fields.get("eprint") or None,
                venue_name=_strip_braces(fields.get("journal") or fields.get("booktitle")),
            )
        )
    return entries


def _strip_braces(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(value.replace("{", "").replace("}", "").split()) or None
