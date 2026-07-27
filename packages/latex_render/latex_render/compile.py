"""texd 编译客户端 + 有界自动修复（设计 §4.6）。

编译失败自动修复（有界 ≤2 轮）：
1. **确定性修复优先**：转义漏网特殊字符、去掉未定义环境、降级缺失宏包；
2. 其后才允许 LLM 针对报错行做最小修补（调用方注入 `patcher`）；
3. 仍失败 → 交付 LaTeX 工程 + Markdown 预览并展示日志（draft-first，绝不阻断）。
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

MAX_REPAIR_ROUNDS = 2

# 允许 LLM 自由书写 LaTeX 的环境（设计 §4.5）；其余一律视为未定义环境处理。
ALLOWED_ENVIRONMENTS = frozenset(
    {
        "equation",
        "equation*",
        "align",
        "align*",
        "gather",
        "gather*",
        "itemize",
        "enumerate",
        "description",
        "figure",
        "figure*",
        "table",
        "tabular",
        "longtable",
        "algorithm",
        "algorithmic",
        "abstract",
        "document",
        "quote",
        "verbatim",
        "IEEEkeywords",
        # 书目兜底自己会写 thebibliography：不列进白名单，修复轮次会把它当
        # 「未定义环境」剥掉——正好把刚补上的参考文献表又删掉。
        "thebibliography",
    }
)

# 书目**真的没排出来**的信号（Tectonic 把 BibTeX 的错误降级成 warning 后继续
# 走完，因此「编译成功 + 全文 [?]」是完全可能的，必须主动识别）：
#   warning: open of input unsrt.bst failed        ← 取不到 .bst（只读缓存/无外网）
#   I couldn't open style file unsrt.bst           ← 同上，BibTeX 侧的说法
#   LaTeX Warning: Citation `foo2020bar' ... undefined
#
# 刻意**不**把 `errors were issued by BibTeX` 当作充分信号：BibTeX 的错误未必
# 影响书目（实测 gbt7714 模板重复发 \bibstyle 会报一个 error，但 .bbl 完好、
# PDF 里编号引用一切正常）。凭它降级会把投稿方 .bst 的排版换成内联兜底，
# 还给用户挂上一个假的「降级」告警——比不修更糟。
_BIBTEX_FAILURE_RES = (
    re.compile(r"open of input \S*\.bst failed", re.IGNORECASE),
    re.compile(r"I couldn't open style file", re.IGNORECASE),
    re.compile(r"Citation [`'][^'\n]+' (?:on page \d+ )?undefined", re.IGNORECASE),
)
# `\bibliography{refs}`——把它换成内联 thebibliography 就绕开了整条 BibTeX 通路。
# 不动同处的 `\bibliographystyle`：模板里它可能包在 `\IfFileExists{}{}{}` 的分支
# 里，用 `%` 注释会连带吃掉后面的右花括号，把导言区搞坏。它本身不打开 .bst
# （只有 bibtex 程序读 .bst），留着无害。
_BIB_DATA_RE = re.compile(r"\\bibliography\{[^}]*\}")
_BEGIN_DOCUMENT_RE = re.compile(r"\\begin\{document\}")

# natbib 兼容垫片。gbt7714 会加载 natbib，而 natbib 在作者-年份模式下拒绝
# 没有 author-year 元数据的 `\bibitem`——报错点在 **读 .aux 时**，所以垫片
# 必须落在导言区，写进 body 已经晚了（实测：`main.aux:78: Package natbib
# Error: Bibliography not compatible with author-year citations`，编译直接挂，
# 兜底反而被判定为「把编译搞挂」而弃用）。
# 未加载 natbib 的模板（article / IEEEtran）不受影响。
_NATBIB_SHIM = """%% [paperforge] 内联书目兜底的 natbib 垫片：切到数字引用，
%% 否则读 .aux 时 natbib 会因 \\bibitem 缺 author-year 元数据而报错。
\\makeatletter
\\@ifpackageloaded{natbib}{\\setcitestyle{numbers,square}}{}
\\makeatother
"""

_MISSING_PACKAGE_RE = re.compile(r"LaTeX Error: File `([^']+)\.sty' not found", re.IGNORECASE)
_UNDEFINED_ENV_RE = re.compile(r"LaTeX Error: Environment ([A-Za-z*]+) undefined", re.IGNORECASE)
# 日志里的控制序列只有一个反斜杠：`l.5 \madeupcommand`。
_UNDEFINED_CONTROL_RE = re.compile(r"Undefined control sequence.*?\\([A-Za-z@]+)", re.DOTALL)
_ERROR_LINE_RE = re.compile(r"^l\.(\d+)\s*(.*)$", re.MULTILINE)
_TECTONIC_ERROR_LINE_RE = re.compile(
    r"^error:\s+([^:\n]+):(\d+):\s*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class CompileOutcome:
    ok: bool = False
    pdf: bytes | None = None
    log: str = ""
    rounds: int = 0
    repairs: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    # 书目是否真的排出来了。`ok=True` 不含这一层：BibTeX 失败会被 Tectonic
    # 降级为 warning，PDF 照样产出，只是每个引用都变成 `[?]`。
    bibliography_ok: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rounds": self.rounds,
            "pdf_bytes": len(self.pdf) if self.pdf else 0,
            "repairs": self.repairs,
            "bibliography_ok": self.bibliography_ok,
            "log_tail": self.log[-2000:],
        }


def bibliography_broken(log: str) -> bool:
    """日志是否表明书目没排出来（.bst 取不到 / 引用全未定义）。"""
    return any(pattern.search(log) for pattern in _BIBTEX_FAILURE_RES)


def with_inline_bibliography(
    files: dict[str, str],
    block: str,
    *,
    entrypoint: str = "main.tex",
) -> dict[str, str] | None:
    """把 `\\bibliography{refs}` 换成内联 ``thebibliography``；无处可换时返回 None。

    同时在导言区插入 natbib 垫片——见 ``_NATBIB_SHIM``。
    """
    main = files.get(entrypoint)
    if not main or not block or not _BIB_DATA_RE.search(main):
        return None
    replaced = _BIB_DATA_RE.sub(lambda _: block, main, count=1)
    replaced = _BEGIN_DOCUMENT_RE.sub(
        lambda match: _NATBIB_SHIM + match.group(0), replaced, count=1
    )
    patched = dict(files)
    patched[entrypoint] = replaced
    return patched


class TexdClient:
    """texd 编译沙箱客户端（无外网、只读模板、资源限额由 compose 保证）。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 180.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            # 编译服务在本地网络内，显式关闭代理（代理会把 127.0.0.1 请求变成 502）。
            self._client = httpx.Client(timeout=self.timeout_seconds, trust_env=False)
        return self._client

    def compile(
        self,
        files: dict[str, str],
        *,
        entrypoint: str = "main.tex",
        binary_files: dict[str, bytes] | None = None,
    ) -> CompileOutcome:
        try:
            response = self.client.post(
                f"{self.base_url}/compile",
                json={
                    "text_files": files,
                    "binary_files": {
                        path: base64.b64encode(content).decode("ascii")
                        for path, content in (binary_files or {}).items()
                    },
                    "entrypoint": entrypoint,
                },
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as error:
            return CompileOutcome(ok=False, log=f"texd unreachable: {type(error).__name__}")
        if response.status_code != 200:
            return CompileOutcome(
                ok=False,
                log=f"texd returned HTTP {response.status_code}: {response.text[:500]}",
            )
        payload = response.json()
        pdf_b64 = payload.get("pdf_base64")
        return CompileOutcome(
            ok=bool(payload.get("ok")),
            pdf=base64.b64decode(pdf_b64) if pdf_b64 else None,
            log=str(payload.get("log") or ""),
            files=files,
        )


def compile_with_repair(
    files: dict[str, str],
    *,
    client: TexdClient,
    entrypoint: str = "main.tex",
    patcher: Callable[[dict[str, str], str], dict[str, str] | None] | None = None,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    inline_bibliography: str | None = None,
    binary_files: dict[str, bytes] | None = None,
) -> CompileOutcome:
    """编译 + 有界修复。永不抛出：失败也返回带日志的结果（draft-first）。

    `inline_bibliography` 是确定性生成的 ``thebibliography`` 兜底块：BibTeX
    取不到 .bst 时（沙箱缓存只读 / 无外网）会被换进 main.tex 重编一次，
    否则成品 PDF 里每个引用都是 `[?]`——而编译「成功」，没人会发现。
    """
    current = dict(files)
    immutable_binaries = dict(binary_files or {})
    repairs: list[dict[str, Any]] = []
    outcome = _compile_once(client, current, entrypoint, immutable_binaries)
    # 书目降级与语法修复正交，不占用 max_rounds 预算。
    current, outcome = _ensure_bibliography(
        current,
        outcome,
        client=client,
        entrypoint=entrypoint,
        block=inline_bibliography,
        repairs=repairs,
        binary_files=immutable_binaries,
    )
    if outcome.ok:
        outcome.rounds = len(repairs)
        outcome.repairs = list(repairs)
        return outcome

    for round_index in range(1, max_rounds + 1):
        repaired, actions = deterministic_repairs(current, outcome.log)
        if not actions and patcher is not None:
            # 确定性修复无计可施，才轮到 LLM 做最小修补。
            patched = patcher(current, outcome.log)
            if patched:
                repaired = patched
                actions = [{"kind": "llm_patch", "round": round_index}]
        if not actions:
            break
        current = repaired
        outcome = _compile_once(client, current, entrypoint, immutable_binaries)
        repairs.extend(actions)
        if outcome.ok:
            # 语法修好之后书目仍可能是坏的（.bst 取不到与语法错误互不相干）。
            current, outcome = _ensure_bibliography(
                current,
                outcome,
                client=client,
                entrypoint=entrypoint,
                block=inline_bibliography,
                repairs=repairs,
                binary_files=immutable_binaries,
            )
        outcome.rounds = round_index
        outcome.repairs = list(repairs)
        if outcome.ok:
            return outcome
    outcome.files = current
    outcome.repairs = list(repairs)
    return outcome


def _compile_once(
    client: TexdClient,
    files: dict[str, str],
    entrypoint: str,
    binary_files: dict[str, bytes],
) -> CompileOutcome:
    if binary_files:
        outcome = client.compile(files, entrypoint=entrypoint, binary_files=binary_files)
    else:
        outcome = client.compile(files, entrypoint=entrypoint)
    outcome.files = files
    outcome.bibliography_ok = not bibliography_broken(outcome.log)
    return outcome


def _ensure_bibliography(
    current: dict[str, str],
    outcome: CompileOutcome,
    *,
    client: TexdClient,
    entrypoint: str,
    block: str | None,
    repairs: list[dict[str, Any]],
    binary_files: dict[str, bytes],
) -> tuple[dict[str, str], CompileOutcome]:
    """书目坏了就换成内联 ``thebibliography`` 重编一次。"""
    if outcome.bibliography_ok or not block:
        return current, outcome
    patched = with_inline_bibliography(current, block, entrypoint=entrypoint)
    if patched is None:
        return current, outcome
    candidate = _compile_once(client, patched, entrypoint, binary_files)
    # 只在兜底确实修好书目、且没把原本能过的编译搞挂时采纳。
    if candidate.bibliography_ok and (candidate.ok or not outcome.ok):
        repairs.append({"kind": "inline_bibliography", "reason": "bibtex_unavailable"})
        return patched, candidate
    return current, outcome


def deterministic_repairs(
    files: dict[str, str],
    log: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """从编译日志推断确定性修复；返回 (新文件集, 修复动作列表)。"""
    repaired = dict(files)
    actions: list[dict[str, Any]] = []

    for package in dict.fromkeys(_MISSING_PACKAGE_RE.findall(log)):
        main = repaired.get("main.tex", "")
        pattern = re.compile(
            rf"^.*\\usepackage(?:\[[^\]]*\])?\{{[^}}]*\b{re.escape(package)}\b[^}}]*\}}.*$",
            re.MULTILINE,
        )
        if pattern.search(main):
            # 降级缺失宏包：注释掉而不是让整篇编不出来。
            repaired["main.tex"] = pattern.sub(
                lambda match: f"% [paperforge] dropped missing package: {match.group(0).strip()}",
                main,
            )
            actions.append({"kind": "drop_missing_package", "package": package})

    for env in dict.fromkeys(_UNDEFINED_ENV_RE.findall(log)):
        if env in ALLOWED_ENVIRONMENTS:
            continue
        for path, content in list(repaired.items()):
            if f"\\begin{{{env}}}" not in content:
                continue
            # 未定义环境降级为普通段落，保住正文内容。
            replaced = content.replace(f"\\begin{{{env}}}", "").replace(f"\\end{{{env}}}", "")
            repaired[path] = replaced
            actions.append({"kind": "drop_undefined_environment", "environment": env, "file": path})

    for command in dict.fromkeys(_UNDEFINED_CONTROL_RE.findall(log)):
        if command in {"todo"}:
            # \todo 由模板 providecommand 保证；日志里出现说明模板被改坏了。
            continue
        for path, content in list(repaired.items()):
            token = f"\\{command}"
            if token not in content:
                continue
            repaired[path] = content.replace(token, "")
            actions.append({"kind": "drop_undefined_command", "command": command, "file": path})

    return repaired, actions


def error_context(log: str, *, max_items: int = 5) -> list[dict[str, Any]]:
    """从日志抽出报错行号与片段，供 LLM 做最小修补时定位。"""
    items: list[dict[str, Any]] = []
    # Tectonic HTTP 服务的常见形态：
    # ``error: sections/00-s1.tex:42: Missing } inserted``。
    # 旧实现只识别传统 TeX 的 ``l.42 ...``，会让可修复的
    # 章节错误在第 0 轮就停止。
    for match in _TECTONIC_ERROR_LINE_RE.finditer(log):
        items.append(
            {
                "file": match.group(1).strip(),
                "line": int(match.group(2)),
                "snippet": match.group(3)[:200],
            }
        )
        if len(items) >= max_items:
            return items
    for match in _ERROR_LINE_RE.finditer(log):
        items.append({"line": int(match.group(1)), "snippet": match.group(2)[:200]})
        if len(items) >= max_items:
            break
    return items


__all__ = [
    "ALLOWED_ENVIRONMENTS",
    "MAX_REPAIR_ROUNDS",
    "CompileOutcome",
    "TexdClient",
    "bibliography_broken",
    "compile_with_repair",
    "deterministic_repairs",
    "error_context",
    "with_inline_bibliography",
]
