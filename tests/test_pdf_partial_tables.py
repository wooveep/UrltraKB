"""A local detector gap must not erase other proved native cell relationships."""

import pymupdf
import pytest
from test_pdf_tables import Tables, convert


def partial_table_pdf(path):
    with pymupdf.open() as document:
        page = document.new_page()
        # The first header cell is open on its left. All body cells are closed.
        page.draw_line((60, 125), (60, 225))
        for x in (180, 300, 480):
            page.draw_line((x, 100), (x, 225))
        for y in (100, 125, 200, 225):
            page.draw_line((60, y), (480, y))
        for y in (150, 175):
            page.draw_line((180, y), (480, y))
        for x, value in ((65, "Category"), (185, "Feature"), (305, "Description")):
            page.insert_text((x, 118), value)
        for row, feature in enumerate(("alpha", "beta", "gamma", "delta")):
            page.insert_text((185, 143 + row * 25), feature)
            page.insert_text((305, 143 + row * 25), f"condition-{row + 1}")
        page.insert_text((65, 218), "Other")
        page.insert_text((60, 255), "Following exception remains outside the table.")
        document.save(path)
    return path


@pytest.mark.parametrize("mode", ["pages", "markdown"])
def test_partial_native_table_preserves_blank_merged_cell_and_later_category(tmp_path, mode):
    path = partial_table_pdf(tmp_path / "partial.pdf")
    before = path.read_bytes()

    content, _ = convert(path, tmp_path, mode)
    rows = Tables(content).rows

    assert len(rows) == 5
    assert rows[0][0]["data-ultrakb-cell"] == "unresolved"
    assert rows[0][1:] == [{"text": "Feature"}, {"text": "Description"}]
    assert rows[1] == [
        {"text": "", "rowspan": "3"},
        {"text": "alpha"},
        {"text": "condition-1"},
    ]
    assert rows[2] == [{"text": "beta"}, {"text": "condition-2"}]
    assert rows[3] == [{"text": "gamma"}, {"text": "condition-3"}]
    assert rows[4] == [
        {"text": "Other"},
        {"text": "delta"},
        {"text": "condition-4"},
    ]
    assert "Native table layout is partial" in content
    assert "Unresolved boundary" in rows[0][0]["text"]
    for word in ("Category", "alpha", "beta", "gamma", "delta", "Other"):
        assert content.count(word) == 1
    assert content.index("Category") < content.index("<table")
    assert content.index("</table>") < content.index("Following exception")
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["pages", "markdown"])
def test_native_grid_limit_retains_all_text_with_an_explicit_layout_gap(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setattr("ultrakb.pdf_layout._MAX_TABLE_SLOTS", 8)
    path = partial_table_pdf(tmp_path / "limited.pdf")

    content, _ = convert(path, tmp_path, mode)

    assert not Tables(content).rows
    assert "Native table structure exceeds processing bounds" in content
    assert "cell membership is unconfirmed" in content
    for word in ("Category", "Feature", "Description", "alpha", "beta", "gamma", "delta", "Other"):
        assert content.count(word) == 1
