from __future__ import annotations

import base64
import binascii
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI

# Tectonic 编译沙箱（方案 §4.6）：独立容器、无外网、只读模板、资源限额。
# 接收 LaTeX 工程（多文件），Tectonic 编译为 PDF，返回 PDF + 日志。
# 沙箱隔离由 compose 保证（internal 网络、read_only、mem/cpu limit、tmpfs）。
from observability.admission import AdmissionMiddleware
from pydantic import BaseModel, Field, model_validator

app = FastAPI(title="PaperForge texd", version="0.1.0")


class CompileRequest(BaseModel):
    text_files: dict[str, str] = Field(default_factory=dict)
    binary_files: dict[str, str] = Field(default_factory=dict)
    # 旧客户端兼容字段；新客户端只发送 text_files。
    files: dict[str, str] | None = None
    entrypoint: str = "main.tex"

    @model_validator(mode="after")
    def merge_legacy_files(self) -> CompileRequest:
        if self.files:
            if self.text_files:
                raise ValueError("send either files or text_files, not both")
            self.text_files = dict(self.files)
        return self


class CompileResult(BaseModel):
    ok: bool
    log: str
    pdf_base64: str | None = None


app.add_middleware(AdmissionMiddleware, limit=1)


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


MAX_BINARY_FILES = 32
MAX_BINARY_FILE_BYTES = 16 * 1024 * 1024
MAX_BINARY_TOTAL_BYTES = 64 * 1024 * 1024
_BINARY_MAGIC = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".pdf": (b"%PDF-",),
}


def _decode_binary_files(encoded_files: dict[str, str]) -> tuple[dict[str, bytes], str | None]:
    if len(encoded_files) > MAX_BINARY_FILES:
        return {}, f"too many binary files: maximum is {MAX_BINARY_FILES}"
    decoded: dict[str, bytes] = {}
    total = 0
    for rel, encoded in encoded_files.items():
        suffix = Path(rel).suffix.lower()
        signatures = _BINARY_MAGIC.get(suffix)
        if signatures is None:
            return {}, f"unsupported binary extension: {suffix or '(none)'}"
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return {}, f"invalid base64 binary file: {rel}"
        if not data or len(data) > MAX_BINARY_FILE_BYTES:
            return {}, f"binary file exceeds 16 MiB or is empty: {rel}"
        if not any(data.startswith(signature) for signature in signatures):
            return {}, f"binary extension and magic bytes disagree: {rel}"
        total += len(data)
        if total > MAX_BINARY_TOTAL_BYTES:
            return {}, "binary files exceed 64 MiB total"
        decoded[rel] = data
    return decoded, None


@app.post("/compile", response_model=CompileResult)
def compile_project(req: CompileRequest) -> CompileResult:
    """Compile in FastAPI's sync worker pool so health checks stay responsive."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        binaries, binary_error = _decode_binary_files(req.binary_files)
        if binary_error:
            return CompileResult(ok=False, log=binary_error)
        if set(req.text_files) & set(binaries):
            return CompileResult(ok=False, log="path supplied as both text and binary")
        for rel, content in req.text_files.items():
            try:
                path = _resolve_project_path(root, rel)
            except ValueError as exc:
                return CompileResult(ok=False, log=str(exc))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for rel, content in binaries.items():
            try:
                path = _resolve_project_path(root, rel)
            except ValueError as exc:
                return CompileResult(ok=False, log=str(exc))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        try:
            entry = _resolve_project_path(root, req.entrypoint)
        except ValueError as exc:
            return CompileResult(ok=False, log=str(exc))
        if req.entrypoint not in req.text_files or not entry.is_file():
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
