from __future__ import annotations

import re
from hashlib import sha256

# LaTeX 特殊字符转义（确定性代码管格式：LLM 只产内容，转义由代码保证）。
# 用于 paragraph text run；equation/algorithm 块的 latex 不在此转义（走环境白名单校验）。

_LATEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def latex_escape(text: str) -> str:
    out: list[str] = []
    for ch in text:
        out.append(_LATEX_SPECIAL.get(ch, ch))
    return "".join(out)


def latex_identifier(value: str, *, prefix: str = "id") -> str:
    """Return a defensive, injection-safe LaTeX label or cite key."""
    normalized = re.sub(r"[^0-9A-Za-z:._+-]+", "-", value).strip("-")
    if normalized and re.match(r"[0-9A-Za-z]", normalized):
        return normalized
    digest = sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"
