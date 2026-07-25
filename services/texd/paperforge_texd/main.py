from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

# Tectonic 编译沙箱（方案 §4.6）：独立容器、无外网、只读模板、资源限额。
# 接收 LaTeX 工程（多文件），Tectonic 编译为 PDF，返回 PDF + 日志。
# 沙箱隔离由 compose 保证（internal 网络、read_only、mem/cpu limit、tmpfs）。

app = FastAPI(title="PaperForge texd", version="0.1.0")


class CompileRequest(BaseModel):
    # 相对路径 → 文件内容（main.tex、sections/*.tex、refs.bib、figures 以 base64 另行处理）
    files: dict[str, str]
    entrypoint: str = "main.tex"


class CompileResult(BaseModel):
    ok: bool
    log: str
    pdf_base64: str | None = None


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _resolve_project_path(root: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError(f"unsafe path: {relative_path}")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative_path).resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise ValueError(f"unsafe path: {relative_path}")
    return resolved


@app.post("/compile", response_model=CompileResult)
def compile_project(req: CompileRequest) -> CompileResult:
    """Compile in FastAPI's sync worker pool so health checks stay responsive."""
    import base64

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, content in req.files.items():
            try:
                path = _resolve_project_path(root, rel)
            except ValueError as exc:
                return CompileResult(ok=False, log=str(exc))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        try:
            entry = _resolve_project_path(root, req.entrypoint)
        except ValueError as exc:
            return CompileResult(ok=False, log=str(exc))
        if req.entrypoint not in req.files or not entry.is_file():
            return CompileResult(ok=False, log=f"entrypoint not supplied: {req.entrypoint}")
        try:
            proc = subprocess.run(
                ["tectonic", "--outdir", str(root), str(entry)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except FileNotFoundError:
            return CompileResult(ok=False, log="tectonic not installed in this container")
        except subprocess.TimeoutExpired:
            return CompileResult(ok=False, log="compile timed out")
        pdf = entry.with_suffix(".pdf")
        log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode == 0 and pdf.exists():
            return CompileResult(
                ok=True, log=log, pdf_base64=base64.b64encode(pdf.read_bytes()).decode()
            )
        return CompileResult(ok=False, log=log)
