import io
import math

import pytest
from ingest.research import analyze, derived_table, read_table
from openpyxl import Workbook


def test_descriptive_statistics_and_provenance_table():
    table = read_table(b"group,x\na,1\na,3\na,\nb,2\n", "data.csv")
    result = analyze(table, {"columns": ["x"], "group_by": "group", "units": {"x": "ms"}})
    first, second = result["records"]
    assert first["n"] == 2 and first["missing"] == 1
    assert first["mean"] == first["median"] == 2
    assert first["std"] == pytest.approx(math.sqrt(2))
    assert second["std"] is None
    derived = derived_table(result)
    assert "1.414213562" in derived["numbers"]
    assert "不可计算" not in derived["numbers"]


@pytest.mark.parametrize("data", [b"x,x\n1,2\n", b"x,y\n1\n", b"x\n", b"x\n\xff\n"])
def test_rejects_ambiguous_tables(data):
    with pytest.raises(ValueError):
        read_table(data, "data.csv")


@pytest.mark.parametrize("value", ["NaN", "inf", "3 ms", "-Infinity"])
def test_invalid_numbers_are_not_silently_dropped(value):
    with pytest.raises(ValueError):
        analyze(
            read_table(f"x\n{value}\n".encode(), "a.csv"), {"columns": ["x"], "units": {"x": "1"}}
        )


def test_xlsx_requires_explicit_sheet_and_rejects_formulas():
    book = Workbook()
    book.active.append(["x"])
    book.active.append([3])
    book.create_sheet("second").append(["x"])
    data = io.BytesIO()
    book.save(data)
    assert read_table(data.getvalue(), "a.xlsx")["needs_sheet"]
    assert read_table(data.getvalue(), "a.xlsx", "Sheet")["rows"] == [["3"]]
    book.active["A2"] = "=1+1"
    data = io.BytesIO()
    book.save(data)
    with pytest.raises(ValueError, match="公式"):
        read_table(data.getvalue(), "a.xlsx", "Sheet")
