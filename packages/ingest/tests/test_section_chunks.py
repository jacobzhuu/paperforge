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


def test_parse_markdown_table_accepts_a_caption_prefix_and_retains_offsets():
    text = "Table 2. Main results\n| Method | F1 |\n| --- | --- |\n| Ours | 91.3 |\nNotes."
    table = parse_markdown_table(text)
    assert table is not None
    ours = next(cell for cell in table.cells if cell.value.strip() == "91.3")
    assert text[ours.start_offset : ours.end_offset].strip() == "91.3"
