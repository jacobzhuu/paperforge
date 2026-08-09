from __future__ import annotations

import io
import tarfile

from ingest import extract_content


def test_jats_extracts_sections_tables_figures_and_equations() -> None:
    content = b"""<?xml version="1.0"?>
    <article>
      <front><article-meta>
        <title-group><article-title>Evidence-rich Study</article-title></title-group>
        <permissions><license><license-p>CC BY 4.0</license-p></license></permissions>
        <abstract><p>We evaluate a model.</p></abstract>
      </article-meta></front>
      <body>
        <sec id="s1"><title>Results</title>
          <p>The model improves F1 on TestSet.</p>
          <table-wrap id="T2"><label>Table 2</label><caption><p>Main results</p></caption>
            <table><tr><th>Dataset</th><th>F1</th></tr>
            <tr><td>TestSet</td><td>91.3</td></tr></table>
          </table-wrap>
          <fig id="F1"><label>Figure 1</label><caption><p>System overview</p></caption></fig>
          <disp-formula id="E1"><label>(1)</label><tex-math>E = mc^2</tex-math></disp-formula>
        </sec>
      </body>
    </article>"""
    parsed = extract_content(mime_type="application/jats+xml", content=content)
    assert parsed.title == "Evidence-rich Study"
    assert "91.3" in parsed.text
    assert any(
        segment.get("section_title") == "Results"
        for segment in parsed.metadata["structure_segments"]
    )
    refs = {item["object_ref"] for item in parsed.metadata["structured_objects"]}
    assert {"table:T2", "fig:F1", "eq:E1"} <= refs
    assert parsed.metadata["license"] == "CC BY 4.0"


def test_pmc_oai_wrapper_is_accepted_as_jats() -> None:
    content = b"""<?xml version="1.0"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <GetRecord><record><metadata>
        <article xmlns=""><front><article-meta><title-group>
          <article-title>Wrapped article</article-title>
        </title-group></article-meta></front>
        <body><sec><title>Methods</title><p>We enrolled 120 participants.</p></sec></body>
        </article>
      </metadata></record></GetRecord>
    </OAI-PMH>"""
    parsed = extract_content(mime_type="application/xml", content=content)
    assert parsed.title == "Wrapped article"
    assert "120 participants" in parsed.text


def test_arxiv_source_extracts_includes_table_equation_and_caption() -> None:
    main = r"""
    \documentclass{article}
    \title{Structured Source}
    \begin{document}
    \begin{abstract}We study retrieval systems.\end{abstract}
    \section{Methods}
    \input{results}
    \end{document}
    """
    included = r"""
    We evaluate the method on TestSet with 100 samples.
    \begin{table}
      \caption{Main results}\label{tab:main}
      \begin{tabular}{lr}
      Dataset & F1 \\
      \hline
      TestSet & 91.3 \\
      \end{tabular}
    \end{table}
    \begin{equation}\label{eq:loss}L = -\log p(y|x)\end{equation}
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, value in (("main.tex", main), ("results.tex", included)):
            encoded = value.encode()
            info = tarfile.TarInfo(name)
            info.size = len(encoded)
            archive.addfile(info, io.BytesIO(encoded))
    parsed = extract_content(
        mime_type="application/x-arxiv-source",
        content=buffer.getvalue(),
    )
    assert parsed.title == "Structured Source"
    assert "91.3" in parsed.text
    refs = {item["object_ref"] for item in parsed.metadata["structured_objects"]}
    assert "table:tab:main" in refs
    assert "eq:eq:loss" in refs
