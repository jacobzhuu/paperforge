from ingest import ContentRole, classify_content_role, parse_markdown_table


def test_classify_returns_a_known_role():
    text = "[1] A. Smith, B. Jones. A Survey of Methods. Journal of Things, 2020."
    decision = classify_content_role(text)
    assert isinstance(decision.role, ContentRole)


def test_parse_markdown_table_extracts_columns_and_cells():
    text = "| Method | Acc |\n| --- | --- |\n| Ours | 0.91 |\n| Base | 0.85 |"
    table = parse_markdown_table(text)
    assert table is not None
    assert table.columns == ("Method", "Acc")
    assert len(table.cells) == 4
