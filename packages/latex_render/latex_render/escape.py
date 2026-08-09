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

# ---------------------------------------------------------------------------
# Unicode → LaTeX 兜底
#
# Latin Modern（Tectonic 的默认正文字体）没有下标/上标/希腊字母/数学算符的字形。
# 缺字形时 TeX **不报错**，只发一条 warning 然后把字符**整个丢掉**：
#   warning: Missing character: There is no ₂ (U+2082) in font [lmroman10-regular]
# 于是 "H₂O 在 π/4 处" 会静默变成 "HO 在 /4 处"——编译成功、PDF 拿得到、
# 数字却错了。这是最危险的一类失败，必须在渲染前把它们换成等价的 LaTeX。
#
# LLM 写正文时用这些字符非常普遍（尤其是化学式与单位），因此这是常态路径。
# ---------------------------------------------------------------------------

# fmt: off
# 查找表按行分组比一行一个键可读得多（下标 0-9 一行、修饰符一行、字母一行）。
_SUBSCRIPTS = {
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
    "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
    "₊": "+", "₋": "-", "₌": "=", "₍": "(", "₎": ")",
    "ₐ": "a", "ₑ": "e", "ₒ": "o", "ₓ": "x", "ₕ": "h", "ₖ": "k", "ₗ": "l",
    "ₘ": "m", "ₙ": "n", "ₚ": "p", "ₛ": "s", "ₜ": "t", "ᵢ": "i", "ⱼ": "j",
}
_SUPERSCRIPTS = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "⁺": "+", "⁻": "-", "⁼": "=", "⁽": "(", "⁾": ")",
    "ⁿ": "n", "ⁱ": "i",
}
_MATH_SYMBOLS = {
    # 希腊字母
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\varepsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu",
    "ν": r"\nu", "ξ": r"\xi", "π": r"\pi", "ρ": r"\rho",
    "σ": r"\sigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\varphi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
    "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Υ": r"\Upsilon",
    "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega",
    # 关系与算符
    "×": r"\times", "÷": r"\div", "±": r"\pm", "∓": r"\mp",
    "≈": r"\approx", "≠": r"\neq", "≡": r"\equiv", "≃": r"\simeq",
    "≤": r"\leq", "≥": r"\geq", "≪": r"\ll", "≫": r"\gg",
    "∞": r"\infty", "∑": r"\sum", "∏": r"\prod", "∫": r"\int",
    "√": r"\surd", "∂": r"\partial", "∇": r"\nabla",
    "∈": r"\in", "∉": r"\notin", "⊂": r"\subset", "⊃": r"\supset",
    "⊆": r"\subseteq", "⊇": r"\supseteq", "∪": r"\cup", "∩": r"\cap",
    "→": r"\rightarrow", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "⇒": r"\Rightarrow", "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow",
    "·": r"\cdot", "∼": r"\sim", "∝": r"\propto", "∅": r"\emptyset",
    "∀": r"\forall", "∃": r"\exists", "¬": r"\neg", "⊕": r"\oplus",
    "⊗": r"\otimes", "∠": r"\angle", "∥": r"\parallel", "⊥": r"\perp",
    "−": "-", "∘": r"\circ", "°": r"^\circ", "′": r"'", "″": r"''",
    "µ": r"\mu", "℃": r"^\circ\mathrm{C}", "℉": r"^\circ\mathrm{F}",
    "Å": r"\mathrm{\AA}", "ℓ": r"\ell", "ħ": r"\hbar",
}
# 纯文本替换（不进数学模式）。
_TEXT_SYMBOLS = {
    " ": "~",  # 不换行空格：TeX 里 `~` 就是它的语义
    " ": r"\,",  # thin space
    " ": r"\,",  # narrow no-break space
    "…": r"\ldots{}",
    "€": r"\texteuro{}",
    "™": r"\texttrademark{}",
    "©": r"\textcopyright{}",
    "®": r"\textregistered{}",
}
# fmt: on
# 零宽字符：没有任何字形，留着只会刷 warning。直接删。
_ZERO_WIDTH = dict.fromkeys("​‌‍⁠﻿", "")


def _char_class(*tables: dict[str, str]) -> str:
    return "[" + "".join(re.escape(ch) for table in tables for ch in table) + "]"


_SUBSCRIPT_RUN_RE = re.compile(_char_class(_SUBSCRIPTS) + "+")
_SUPERSCRIPT_RUN_RE = re.compile(_char_class(_SUPERSCRIPTS) + "+")
_MATH_SYMBOL_RE = re.compile(_char_class(_MATH_SYMBOLS))
_TEXT_REPLACEMENTS = {**_TEXT_SYMBOLS, **_ZERO_WIDTH}
_TEXT_SYMBOL_RE = re.compile(_char_class(_TEXT_REPLACEMENTS))


def _unicode_to_latex(text: str) -> str:
    """把正文字体排不出的 Unicode 换成等价 LaTeX，避免字符被静默丢弃。"""
    # 连续的下标/上标要合并成一组：`x₁₂` 必须是 `$_{12}$`，
    # 逐字符替换会得到 `$_1$$_2$`——双下标，TeX 直接报 `Double subscript`。
    text = _SUBSCRIPT_RUN_RE.sub(
        lambda m: "$_{" + "".join(_SUBSCRIPTS[ch] for ch in m.group(0)) + "}$", text
    )
    text = _SUPERSCRIPT_RUN_RE.sub(
        lambda m: "$^{" + "".join(_SUPERSCRIPTS[ch] for ch in m.group(0)) + "}$", text
    )
    text = _MATH_SYMBOL_RE.sub(lambda m: f"${_MATH_SYMBOLS[m.group(0)]}$", text)
    return _TEXT_SYMBOL_RE.sub(lambda m: _TEXT_REPLACEMENTS[m.group(0)], text)


def latex_escape(text: str) -> str:
    out: list[str] = []
    for ch in text:
        out.append(_LATEX_SPECIAL.get(ch, ch))
    # 先转义、再做 Unicode 兜底：兜底产出的 `$`/`\`/`{}` 是最终 LaTeX，
    # 反过来做会被转义器当成正文字符再转一次。
    return _unicode_to_latex("".join(out))


def latex_identifier(value: str, *, prefix: str = "id") -> str:
    """Return a defensive, injection-safe LaTeX label or cite key."""
    normalized = re.sub(r"[^0-9A-Za-z:._+-]+", "-", value).strip("-")
    if normalized and re.match(r"[0-9A-Za-z]", normalized):
        return normalized
    digest = sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"
