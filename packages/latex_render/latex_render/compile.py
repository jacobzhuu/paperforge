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
        "table",
        "tabular",
        "algorithm",
        "algorithmic",
        "abstract",
        "document",
        "quote",
        "verbatim",
        "IEEEkeywords",
    }
)

_MISSING_PACKAGE_RE = re.compile(r"LaTeX Error: File `([^']+)\.sty' not found", re.IGNORECASE)
_UNDEFINED_ENV_RE = re.compile(r"LaTeX Error: Environment ([A-Za-z*]+) undefined", re.IGNORECASE)
# 日志里的控制序列只有一个反斜杠：`l.5 \madeupcommand`。
_UNDEFINED_CONTROL_RE = re.compile(r"Undefined control sequence.*?\\([A-Za-z@]+)", re.DOTALL)
_ERROR_LINE_RE = re.compile(r"^l\.(\d+)\s*(.*)$", re.MULTILINE)


@dataclass
class CompileOutcome:
    ok: bool = False
    pdf: bytes | None = None
    log: str = ""
    rounds: int = 0
    repairs: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rounds": self.rounds,
            "pdf_bytes": len(self.pdf) if self.pdf else 0,
            "repairs": self.repairs,
            "log_tail": self.log[-2000:],
        }


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

    def compile(self, files: dict[str, str], *, entrypoint: str = "main.tex") -> CompileOutcome:
        try:
            response = self.client.post(
                f"{self.base_url}/compile",
                json={"files": files, "entrypoint": entrypoint},
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
) -> CompileOutcome:
    """编译 + 有界修复。永不抛出：失败也返回带日志的结果（draft-first）。"""
    current = dict(files)
    outcome = client.compile(current, entrypoint=entrypoint)
    outcome.files = current
    if outcome.ok:
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
        outcome = client.compile(current, entrypoint=entrypoint)
        outcome.rounds = round_index
        outcome.repairs.extend(actions)
        outcome.files = current
        if outcome.ok:
            return outcome
    outcome.files = current
    return outcome


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
    "compile_with_repair",
    "deterministic_repairs",
    "error_context",
]
