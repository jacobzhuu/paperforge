"""arXiv LaTeX 源码解析器（安全、确定性、无 TeX 执行）。"""

from __future__ import annotations

import gzip
import io
import re
import tarfile
from pathlib import PurePosixPath
from typing import Any

from ingest.document_extractors import DocumentParseError, mime_policy_metadata
from ingest.jats import _TextBuilder
from ingest.types import ParsedContent

MAX_SOURCE_FILES = 256
MAX_SOURCE_BYTES = 32 * 1024 * 1024

_SECTION_RE = re.compile(
    r"\\(?P<level>section|subsection|subsubsection)\*?\s*\{(?P<title>[^{}]{1,300})\}",
    re.IGNORECASE,
)
_ENV_RE = re.compile(
    r"\\begin\{(?P<env>table\*?|figure\*?|equation\*?|align\*?|"
    r"gather\*?|algorithm\*?)\}(?P<body>.*?)\\end\{(?P=env)\}",
    re.IGNORECASE | re.DOTALL,
)
_INCLUDE_RE = re.compile(r"\\(?:input|include)\s*\{([^{}]+)\}")


def extract_latex_source_content(
    *,
    content: bytes,
    mime_type: str = "application/x-arxiv-source",
) -> ParsedContent:
    files = _read_source_files(content)
    if not files:
        raise DocumentParseError("latex_source_empty")
    main_name = _select_main_tex(files)
    document = _expand_includes(main_name, files=files, seen=set())
    document = _strip_comments(document)
    title_match = re.search(r"\\title\s*\{([^{}]{1,500})\}", document, re.DOTALL)
    title = _latex_to_text(title_match.group(1)) if title_match else None
    builder = _TextBuilder()
    structured_objects: list[dict[str, Any]] = []

    sections = list(_SECTION_RE.finditer(document))
    if not sections:
        _append_latex_region(
            document,
            builder=builder,
            section_path="Document",
            structured_objects=structured_objects,
        )
    else:
        preamble_body = document[: sections[0].start()]
        abstract = _environment_body(preamble_body, "abstract")
        if abstract:
            builder.append(_latex_to_text(abstract), section_path="Abstract")
        path: list[str] = []
        for index, match in enumerate(sections):
            level = {"section": 1, "subsection": 2, "subsubsection": 3}[
                match.group("level").casefold()
            ]
            section_title = _latex_to_text(match.group("title"))
            path = path[: level - 1] + [section_title]
            section_path = " / ".join(path)
            builder.append(section_title, section_path=section_path)
            end = sections[index + 1].start() if index + 1 < len(sections) else len(document)
            _append_latex_region(
                document[match.end() : end],
                builder=builder,
                section_path=section_path,
                structured_objects=structured_objects,
            )
    if not builder.text:
        raise DocumentParseError("latex_body_empty")

    return ParsedContent(
        text=builder.text,
        title=title,
        source_type="latex_source",
        metadata={
            **mime_policy_metadata(mime_type),
            "extractor": "latex_source_v1",
            "parser_status": "success",
            "parser_kind": "latex",
            "content_type": mime_type,
            "text_length": len(builder.text),
            "page_locator_reliable": False,
            "source_main_file": main_name,
            "source_file_count": len(files),
            "structure_segments": builder.segments,
            "structured_objects": builder.objects,
        },
    )


def _read_source_files(content: bytes) -> dict[str, str]:
    if len(content) > MAX_SOURCE_BYTES:
        raise DocumentParseError("latex_source_too_large")
    payload = content
    if content.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(content)
        except OSError:
            payload = content
    if payload.startswith((b"ustar",)):
        # A valid tar starts its magic at offset 257, so this branch is only documentary.
        pass
    files: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            for member in archive.getmembers()[:MAX_SOURCE_FILES]:
                path = PurePosixPath(member.name)
                if (
                    not member.isfile()
                    or path.is_absolute()
                    or ".." in path.parts
                    or path.suffix.casefold() not in {".tex", ".ltx"}
                    or member.size > MAX_SOURCE_BYTES
                ):
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                files[str(path)] = extracted.read().decode("utf-8", errors="replace")
    except (tarfile.TarError, OSError):
        decoded = payload.decode("utf-8", errors="replace")
        if "\\document" in decoded or "\\section" in decoded:
            files["main.tex"] = decoded
    return files


def _select_main_tex(files: dict[str, str]) -> str:
    ranked = sorted(
        files,
        key=lambda name: (
            "\\documentclass" in files[name],
            "\\begin{document}" in files[name],
            len(files[name]),
        ),
        reverse=True,
    )
    if not ranked:
        raise DocumentParseError("latex_main_file_missing")
    return ranked[0]


def _expand_includes(name: str, *, files: dict[str, str], seen: set[str]) -> str:
    if name in seen:
        return ""
    seen.add(name)
    text = files.get(name, "")
    base = PurePosixPath(name).parent

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        candidate = base / raw
        if candidate.suffix.casefold() not in {".tex", ".ltx"}:
            candidate = candidate.with_suffix(".tex")
        normalized = str(candidate)
        if ".." in candidate.parts or normalized not in files:
            return ""
        return _expand_includes(normalized, files=files, seen=seen)

    return _INCLUDE_RE.sub(replace, text)


def _append_latex_region(
    region: str,
    *,
    builder: _TextBuilder,
    section_path: str,
    structured_objects: list[dict[str, Any]],
) -> None:
    del structured_objects  # builder owns the normalized object registry
    cursor = 0
    for index, match in enumerate(_ENV_RE.finditer(region), start=1):
        _append_latex_prose(region[cursor : match.start()], builder, section_path)
        env = match.group("env").rstrip("*").casefold()
        body = match.group("body")
        kind = _environment_kind(env)
        object_ref = _latex_object_ref(body, kind=kind, fallback=index)
        if kind == "equation" and env in {"align", "gather"}:
            inner = "aligned" if env == "align" else "gathered"
            body = f"\\begin{{{inner}}}{body}\\end{{{inner}}}"
        rendered = _render_environment(body, kind=kind)
        builder.append(
            rendered,
            section_path=section_path,
            object_ref=object_ref,
            object_kind=kind,
        )
        cursor = match.end()
    _append_latex_prose(region[cursor:], builder, section_path)


def _append_latex_prose(region: str, builder: _TextBuilder, section_path: str) -> None:
    cleaned = re.sub(r"\\begin\{(?:document|abstract)\}|\\end\{(?:document|abstract)\}", "", region)
    text = _latex_to_text(cleaned)
    for paragraph in re.split(r"\n\s*\n", text):
        if len(paragraph.strip()) >= 20:
            builder.append(paragraph, section_path=section_path)


def _render_environment(body: str, *, kind: str) -> str:
    caption_match = re.search(r"\\caption\s*\{([^{}]{1,1000})\}", body, re.DOTALL)
    caption = _latex_to_text(caption_match.group(1)) if caption_match else ""
    if kind == "table":
        tabular = re.search(
            r"\\begin\{tabular\*?\}(?:\[[^\]]*\])?\{[^{}]*\}(.*?)"
            r"\\end\{tabular\*?\}",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        table = _tabular_to_markdown(tabular.group(1)) if tabular else _latex_to_text(body)
        return f"{caption}\n{table}".strip()
    if kind == "equation":
        equation = re.sub(r"\\label\s*\{[^{}]+\}", "", body)
        return f"{caption}\n$$\n{equation.strip()}\n$$".strip()
    return f"{caption}\n{_latex_to_text(body)}".strip()


def _tabular_to_markdown(body: str) -> str:
    cleaned = re.sub(r"\\(?:toprule|midrule|bottomrule|hline|cline\{[^{}]+\})", "", body)
    rows = [
        [_latex_to_text(cell) for cell in row.split("&")]
        for row in re.split(r"\\\\(?:\[[^\]]*\])?", cleaned)
        if row.strip()
    ]
    rows = [row for row in rows if any(row)]
    if not rows:
        return _latex_to_text(body)
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header, *data = normalized
    return "\n".join(
        [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
            *["| " + " | ".join(row) + " |" for row in data],
        ]
    )


def _latex_object_ref(body: str, *, kind: str, fallback: int) -> str:
    label = re.search(r"\\label\s*\{([^{}]{1,120})\}", body)
    return f"{_object_prefix(kind)}:{label.group(1) if label else fallback}"[:64]


def _environment_kind(env: str) -> str:
    if env == "table":
        return "table"
    if env == "figure":
        return "figure"
    if env == "algorithm":
        return "algorithm"
    return "equation"


def _object_prefix(kind: str) -> str:
    return {"figure": "fig", "equation": "eq", "algorithm": "algo"}.get(kind, kind)


def _environment_body(text: str, environment: str) -> str | None:
    match = re.search(
        rf"\\begin\{{{re.escape(environment)}\}}(.*?)\\end\{{{re.escape(environment)}\}}",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) if match else None


def _strip_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%[^\n]*", "", text)


def _latex_to_text(text: str) -> str:
    math_fragments: list[str] = []

    def keep_math(match: re.Match[str]) -> str:
        math_fragments.append(match.group(0))
        return f"PFMATHFRAGMENT{len(math_fragments) - 1}END"

    text = re.sub(
        r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|(?<![\\$])\$(?!\$)[^$\n]+\$(?!\$)",
        keep_math,
        text,
        flags=re.S,
    )
    value = re.sub(r"\\(?:cite|citep|citet|ref|eqref|label)\s*\{[^{}]*\}", "", text)
    value = re.sub(
        r"\\(?:textbf|textit|emph|mathrm|mathbf|operatorname|url|href)\s*\{([^{}]*)\}",
        r"\1",
        value,
    )
    value = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?", " ", value)
    value = value.replace("{", "").replace("}", "")
    value = value.replace("~", " ")
    lines = [" ".join(line.split()) for line in value.splitlines()]
    value = "\n".join(line for line in lines if line).strip()
    for index, fragment in enumerate(math_fragments):
        value = value.replace(f"PFMATHFRAGMENT{index}END", fragment)
    return value


__all__ = ["extract_latex_source_content"]
