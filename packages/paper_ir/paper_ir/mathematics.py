"""Conservative, shared math validation; never execute source-provided TeX macros."""

from __future__ import annotations

import re

# Only mathematical commands from the packages shipped with our templates.
_COMMANDS = frozenset(
    """frac dfrac tfrac sqrt sum prod int iint iiint oint lim min max arg
sin cos tan log ln exp det dim sup inf argmin argmax left right middle big Big bigg Bigg
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu
nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega
Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
mathbf mathrm mathit mathsf mathtt mathbb mathcal boldsymbol operatorname text
hat widehat bar overline underline vec tilde widetilde dot ddot
cdot times div pm mp ast star circ bullet otimes oplus
le leq ge geq ne neq approx sim simeq equiv propto in notin subset subseteq supset supseteq
cup cap setminus emptyset varnothing forall exists neg land lor lnot
infty partial nabla ell hbar imath jmath Re Im
rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow mapsto to gets
underbrace overbrace underset overset substack limits nolimits
quad qquad space thinspace vert Vert lvert rvert lVert rVert langle rangle
ldots cdots vdots ddots dots mod bmod pmod begin end""".split()
)
_ENVIRONMENTS = frozenset(
    {
        "aligned",
        "gathered",
        "split",
        "cases",
        "matrix",
        "pmatrix",
        "bmatrix",
        "Bmatrix",
        "vmatrix",
        "Vmatrix",
        "smallmatrix",
    }
)


def valid_math(latex: str) -> bool:
    if not isinstance(latex, str) or not latex.strip() or len(latex) > 4000:
        return False
    if re.search(r"[$%#\x00-\x08\x0b\x0c\x0e-\x1f]", latex) or "^^" in latex:
        return False
    depth = 0
    for token in re.findall(r"\\.|[{}]", latex):
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        if depth < 0:
            return False
    if depth:
        return False
    if any(c not in _COMMANDS for c in re.findall(r"\\([A-Za-z]+)", latex)):
        return False
    stack: list[str] = []
    for action, env in re.findall(r"\\(begin|end)\s*\{([^}]+)\}", latex):
        if env not in _ENVIRONMENTS:
            return False
        if action == "begin":
            stack.append(env)
        elif not stack or stack.pop() != env:
            return False
    return not stack


_MATH = re.compile(
    r"\$\$(?P<display>.*?)\$\$|\\\[(?P<bracket>.*?)\\\]|"
    r"\\\((?P<inline>.*?)\\\)|(?<![\\$])\$(?!\$)(?P<dollar>[^$\n]+)\$(?!\$)|"
    r"\\begin\{(?P<env>equation\*?|align\*?|displaymath)\}(?P<body>.*?)"
    r"\\end\{(?P=env)\}",
    re.S,
)


def source_formulas(text: str, *, object_ref: str = "") -> list[dict[str, str]]:
    """Extract complete expressions, never reconstruct one from a formula number."""
    results: list[dict[str, str]] = []
    for match in _MATH.finditer(text):
        latex = next(
            match.group(k)
            for k in ("display", "bracket", "inline", "dollar", "body")
            if match.group(k) is not None
        ).strip()
        latex = re.sub(r"\\label\s*\{[^{}]+\}", "", latex).strip()
        if (match.group("env") or "").startswith("align"):
            latex = r"\begin{aligned}" + latex + r"\end{aligned}"
        if valid_math(latex):
            results.append(
                {
                    "latex": latex,
                    "source_text": match.group(0),
                    "context": text[max(0, match.start() - 600) : match.end() + 1000],
                }
            )
    return results
