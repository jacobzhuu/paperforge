from __future__ import annotations

import base64
import io
import shutil
import zipfile

import pytest
from paperforge_worker.pipelines.export import _markdown_bundle, _markdown_to_docx
from pypdf import PdfWriter


def _png() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
        "AScY42YAAAAASUVORK5CYII="
    )


def _pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=144, height=72)
    writer.write(output)
    return output.getvalue()


def test_markdown_bundle_contains_figures_and_provenance() -> None:
    bundle = _markdown_bundle(
        "# Paper\n\n![figure](figures/va_test.png)\n",
        {"va_test": ("figures/va_test.png", _png())},
        [{"asset_ref": "va_test", "kind": "chart"}],
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == {
            "paper.md",
            "figures/va_test.png",
            "visual-provenance.json",
        }
        assert archive.read("figures/va_test.png").startswith(b"\x89PNG")
        assert b'"kind": "chart"' in archive.read("visual-provenance.json")


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc is not installed")
def test_docx_really_embeds_relative_figure() -> None:
    docx = _markdown_to_docx(
        "# Paper\n\n![accessible figure](figures/va_test.png)\n",
        {"va_test": ("figures/va_test.png", _png())},
    )
    assert docx is not None
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
        assert media
        assert archive.read(media[0]).startswith(b"\x89PNG")


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc is not installed")
def test_docx_converts_uploaded_pdf_figure_to_embedded_png() -> None:
    docx = _markdown_to_docx(
        "# Paper\n\n![PDF figure](figures/ua_test.pdf)\n",
        {"ua_test": ("figures/ua_test.pdf", _pdf())},
    )
    assert docx is not None
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
        assert len(media) == 1
        assert media[0].endswith(".png")
        assert archive.read(media[0]).startswith(b"\x89PNG")
