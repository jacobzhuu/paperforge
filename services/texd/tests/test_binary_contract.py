from __future__ import annotations

import base64

from fastapi.testclient import TestClient
from paperforge_texd.main import _decode_binary_files, app


def test_binary_contract_checks_magic_base64_and_limits() -> None:
    png = b"\x89PNG\r\n\x1a\nrest"
    decoded, error = _decode_binary_files({"figures/a.png": base64.b64encode(png).decode()})
    assert error is None
    assert decoded["figures/a.png"] == png

    assert _decode_binary_files({"figures/a.png": "%%%"})[1] == (
        "invalid base64 binary file: figures/a.png"
    )
    assert "magic bytes disagree" in str(
        _decode_binary_files({"figures/a.png": base64.b64encode(b"%PDF-1.7").decode()})[1]
    )
    assert "unsupported binary extension" in str(
        _decode_binary_files({"figures/a.exe": base64.b64encode(b"MZ").decode()})[1]
    )


def test_binary_path_traversal_is_rejected_before_compile() -> None:
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nrest").decode()
    response = TestClient(app).post(
        "/compile",
        json={
            "text_files": {"main.tex": "\\documentclass{article}"},
            "binary_files": {"../escape.png": png},
            "entrypoint": "main.tex",
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "unsafe path" in response.json()["log"]
