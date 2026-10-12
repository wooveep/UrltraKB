"""Native line-height boxes must not erase otherwise proven PDF table geometry."""

import pymupdf
import pytest


def _source(path, rotation):
    with pymupdf.open() as document:
        page = document.new_page(width=240, height=200)
        for left, top, right, bottom in [
            (20, 60, 200, 60),
            (20, 90, 200, 90),
            (90, 120, 200, 120),
            (20, 150, 200, 150),
            (20, 60, 20, 150),
            (90, 60, 90, 150),
            (145, 60, 145, 150),
            (200, 60, 200, 150),
        ]:
            page.draw_line((left, top), (right, bottom))
        for point, text in [
            ((25, 52), "Before table"),
            ((25, 76), "Category"),
            ((96, 76), "Alpha"),
            ((151, 76), "Beta"),
            ((25, 108), "Nodes"),
            ((100, 108), "3"),
            ((158, 108), "0"),
            ((100, 138), "2"),
            ((158, 138), "1"),
        ]:
            page.insert_text(point, text, fontsize=10)
        page.set_rotation(rotation)
        document.save(path)


def _inflated_boxes(monkeypatch, *, malformed=False, cross_column=False):
    original = pymupdf.Page.get_text

    def extract(page, *args, **kwargs):
        value = original(page, *args, **kwargs)
        if args and args[0] == "dict":
            for block in value["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        x0, _, x1, _ = span["bbox"]
                        y = span["origin"][1]
                        span.update(ascender=2.0, descender=-1.0, bbox=(x0, y - 20, x1, y + 10))
                        if malformed:
                            span["ascender"] = span["descender"]
                        if cross_column and span["text"] == "Alpha":
                            span["bbox"] = (x0, y - 20, 160, y + 10)
        return value

    monkeypatch.setattr(pymupdf.Page, "get_text", extract)


@pytest.mark.parametrize("rotation", [0, 90])
def test_public_pdf_conversion_uses_local_font_metrics_for_cell_membership(
    tmp_path, monkeypatch, rotation
):
    from openkb.images import convert_pdf_to_pages

    source = tmp_path / "native.pdf"
    _source(source, rotation)
    original = source.read_bytes()
    global_setting = pymupdf.TOOLS.set_small_glyph_heights()
    _inflated_boxes(monkeypatch)
    pages = convert_pdf_to_pages(source, "native", tmp_path / "images")
    content = pages[0]["content"]
    assert any(f'<td {attribute}="2">Nodes</td>' in content for attribute in ("rowspan", "colspan"))
    if rotation == 0:
        assert "<td>3</td><td>0</td>" in content
        assert "<td>2</td><td>1</td>" in content
    else:
        assert all(f"<td>{value}</td>" in content for value in ("3", "0", "2", "1"))
    assert content.index("Before table") < content.index("<table>")
    assert source.read_bytes() == original
    assert pymupdf.TOOLS.set_small_glyph_heights() == global_setting


@pytest.mark.parametrize("case", ["invalid_metrics", "horizontal_crossing"])
def test_font_metrics_do_not_authorize_ambiguous_cell_assignments(tmp_path, monkeypatch, case):
    from openkb.images import convert_pdf_to_pages

    source = tmp_path / "native.pdf"
    _source(source, 0)
    _inflated_boxes(
        monkeypatch, malformed=case == "invalid_metrics", cross_column=case == "horizontal_crossing"
    )
    content = convert_pdf_to_pages(source, "native", tmp_path / "images")[0]["content"]
    assert "<table>" not in content
    assert all(word in content for word in ("Before table", "Category", "Nodes", "Alpha", "Beta"))
