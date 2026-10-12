"""Native PDF geometry survives both public text conversion paths."""

from html.parser import HTMLParser

import pymupdf
import pytest

from openkb.images import convert_pdf_to_pages, convert_pdf_with_images


class Tables(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.rows = []
        self.cell = None
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag == "td":
            self.cell = {"text": "", **dict(attrs)}
            self.rows[-1].append(self.cell)

    def handle_endtag(self, tag):
        if tag == "td":
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data


def merged_table_pdf(path):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 70), "Before the table: keep this condition.")
        for x in (60, 180, 300, 420):
            page.draw_line((x, 100), (x, 200))
        for y in (100, 125, 200):
            page.draw_line((60, y), (420, y))
        for y in (150, 175):
            page.draw_line((60, y), (300, y))
        for x, text in ((65, "Item"), (185, "Local"), (305, "Group")):
            page.insert_text((x, 118), text)
        for y, text in ((143, "alpha"), (168, "beta"), (193, "gamma")):
            page.insert_text((65, y), text)
        page.insert_text((305, 157), "shared-group")
        page.insert_text((60, 240), "After the table: keep this exception.")
        doc.save(path)
    return path


def convert(path, tmp_path, mode):
    if mode == "pages":
        pages = convert_pdf_to_pages(path, "document", tmp_path / "images")
        return pages[0]["content"], pages
    pages = []
    body = convert_pdf_with_images(path, "document", tmp_path / "images", page_records=pages)
    assert pages[0]["content"].replace("sources/images/", "images/") in body
    return body, pages


@pytest.mark.parametrize("mode", ["pages", "markdown"])
def test_pdf_preserves_merged_column_and_real_empty_cells(tmp_path, mode):
    path = merged_table_pdf(tmp_path / "table.pdf")
    content, pages = convert(path, tmp_path, mode)

    assert Tables(content).rows == [
        [{"text": "Item"}, {"text": "Local"}, {"text": "Group"}],
        [{"text": "alpha"}, {"text": ""}, {"text": "shared-group", "rowspan": "3"}],
        [{"text": "beta"}, {"text": ""}],
        [{"text": "gamma"}, {"text": ""}],
    ]
    for text in ("Before the table:", "After the table:", "alpha", "shared-group"):
        assert content.count(text) == 1
    assert content.index("Before the table:") < content.index("<table>")
    assert content.index("</table>") < content.index("After the table:")
    assert pages[0]["page"] == 1 and pages[0]["unit_kind"] == "page"


@pytest.mark.parametrize("mode", ["pages", "markdown"])
@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_pdf_preserves_visual_table_membership(tmp_path, mode, rotation):
    path = merged_table_pdf(tmp_path / "rotated-table.pdf")
    with pymupdf.open(path) as doc:
        page = doc[0]
        # Rotate the native content, then restore its appearance using /Rotate.
        page.set_rotation(360 - rotation)
        page.remove_rotation()
        page.set_rotation(rotation)
        doc.saveIncr()
    original = path.read_bytes()

    content, _ = convert(path, tmp_path, mode)

    assert Tables(content).rows == [
        [{"text": "Item"}, {"text": "Local"}, {"text": "Group"}],
        [{"text": "alpha"}, {"text": ""}, {"text": "shared-group", "rowspan": "3"}],
        [{"text": "beta"}, {"text": ""}],
        [{"text": "gamma"}, {"text": ""}],
    ]
    assert content.count("Before the table:") == content.count("After the table:") == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize("mode", ["pages", "markdown"])
def test_late_painted_image_stays_between_its_native_text_neighbors(tmp_path, mode):
    from figure_fixtures import png_bytes

    path = tmp_path / "late-image.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        # Preserve text order; an early-painted footer must not capture the image.
        page.insert_text((60, 800), "Footer painted first")
        page.insert_text((60, 100), "Explanation before the figure")
        page.insert_text((60, 250), "Next section after the figure")
        page.insert_image(pymupdf.Rect(60, 130, 250, 210), stream=png_bytes())
        doc.save(path)
    content, pages = convert(path, tmp_path, mode)

    assert content.index("Explanation before") < content.index("![image]")
    assert content.index("![image]") < content.index("Next section")
    assert content.index("Footer painted first") < content.index("Explanation before")
    assert pages[0]["images"] == [{"path": "sources/images/document/p1_img1.png"}]
    assert (tmp_path / "images/p1_img1.png").is_file()


@pytest.mark.parametrize("mode", ["pages", "markdown"])
@pytest.mark.parametrize("overlap_text", [" ", "X"])
def test_only_visible_text_can_block_geometric_image_placement(tmp_path, mode, overlap_text):
    from figure_fixtures import png_bytes

    path = tmp_path / "overlap.pdf"
    image = pymupdf.Rect(60, 130, 250, 210)
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 800), "Footer painted first")
        page.insert_text((60, 100), "Before the figure")
        page.insert_text((60, 250), "After the figure")
        page.insert_text((249.95, 212), overlap_text)
        page.insert_image(image, stream=png_bytes(), keep_proportion=False)
        native_blocks = page.get_text("dict")["blocks"]
        expected_image = next(block["image"] for block in native_blocks if block["type"] == 1)
        spans = [
            span
            for block in native_blocks
            if block["type"] == 0
            for line in block["lines"]
            for span in line["spans"]
            if span["text"] == overlap_text
        ]
        assert len(spans) == 1 and pymupdf.Rect(spans[0]["bbox"]).intersects(image)
        doc.save(path)
    original = path.read_bytes()

    content, pages = convert(path, tmp_path, mode)

    assert content.index("Footer painted first") < content.index("Before the figure")
    assert content.index("Before the figure") < content.index("After the figure")
    if overlap_text.strip():
        assert content.index("After the figure") < content.index("X") < content.index("![image]")
    else:
        assert content.index("Before the figure") < content.index("![image]")
        assert content.index("![image]") < content.index("After the figure")
    assert len(pages[0]["images"]) == 1
    assert (tmp_path / "images/p1_img1.png").read_bytes() == expected_image
    assert path.read_bytes() == original


@pytest.mark.parametrize("mode", ["pages", "markdown"])
def test_image_can_split_two_lines_in_one_native_text_block(tmp_path, mode):
    from figure_fixtures import png_bytes

    path = tmp_path / "between-lines.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 100), "First line\nSecond line", fontsize=8, lineheight=1.5)
        page.insert_image(
            pymupdf.Rect(60, 102.6, 100, 103.2), stream=png_bytes(), keep_proportion=False
        )
        # Prove this fixture exercises a block containing both native text lines.
        assert len(page.get_text("dict")["blocks"][0]["lines"]) == 2
        doc.save(path)
    content, _ = convert(path, tmp_path, mode)
    assert content.index("First line") < content.index("![image]")
    assert content.index("![image]") < content.index("Second line")


@pytest.mark.parametrize("mode", ["pages", "markdown"])
@pytest.mark.parametrize("placement", ["other-column", "overlapping", "conflicting-neighbors"])
def test_ambiguous_or_unrelated_geometry_does_not_reorder_body_or_image(tmp_path, mode, placement):
    from figure_fixtures import png_bytes

    path = tmp_path / "ambiguous.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        if placement == "conflicting-neighbors":
            page.insert_text((60, 250), "Body first")
            page.insert_text((60, 100), "Body second")
        else:
            page.insert_text((60, 100), "Body first")
            page.insert_text((60, 250), "Body second")
        rectangle = {
            "other-column": (350, 130, 450, 210),
            "overlapping": (60, 230, 150, 270),
            "conflicting-neighbors": (60, 130, 150, 210),
        }[placement]
        page.insert_image(pymupdf.Rect(rectangle), stream=png_bytes(), keep_proportion=False)
        doc.save(path)
    content, _ = convert(path, tmp_path, mode)
    assert content.index("Body first") < content.index("Body second") < content.index("![image]")


@pytest.mark.parametrize("mode", ["pages", "markdown"])
def test_pdf_preserves_colspan_and_literal_cell_text(tmp_path, mode):
    path = tmp_path / "colspan.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(60, 100, 300, 160))
        page.draw_line((60, 130), (300, 130))
        page.draw_line((180, 130), (180, 160))
        page.insert_text((65, 118), "Limits < 3 & > 1")
        page.insert_text((65, 148), "one")
        doc.save(path)
    content, _ = convert(path, tmp_path, mode)
    assert Tables(content).rows == [
        [{"text": "Limits < 3 & > 1", "colspan": "2"}],
        [{"text": "one"}, {"text": ""}],
    ]


@pytest.mark.parametrize("mode", ["pages", "markdown"])
@pytest.mark.parametrize("ambiguity", ["crossing-text", "image-in-cell"])
def test_uncertain_table_keeps_original_text_without_inventing_cells(tmp_path, mode, ambiguity):
    from figure_fixtures import png_bytes

    path = merged_table_pdf(tmp_path / "uncertain.pdf")
    with pymupdf.open(path) as doc:
        if ambiguity == "crossing-text":
            doc[0].insert_text((160, 140), "crosses the boundary")
        else:
            doc[0].insert_image(pymupdf.Rect(200, 130, 220, 145), stream=png_bytes())
        doc.saveIncr()
    content, pages = convert(path, tmp_path, mode)
    assert "<table>" not in content
    for text in ("Before the table:", "After the table:", "alpha", "beta", "gamma", "shared-group"):
        assert content.count(text) == 1
    if ambiguity == "crossing-text":
        assert content.count("crosses the boundary") == 1
    else:
        assert len(pages[0]["images"]) == 1 and content.count("![image]") == 1


@pytest.mark.parametrize("mode", ["pages", "markdown"])
def test_unruled_text_and_physical_blank_pages_remain_unchanged(tmp_path, mode):
    path = tmp_path / "plain.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 100), "First paragraph\nSecond line")
        page.insert_text((60, 180), "Another paragraph")
        doc.new_page()
        doc.set_page_labels([{"startpage": 0, "style": "r", "firstpagenum": 1}])
        doc.save(path)
    if mode == "pages":
        pages = convert_pdf_to_pages(path, "plain", tmp_path / "images")
    else:
        pages = []
        convert_pdf_with_images(path, "plain", tmp_path / "images", page_records=pages)
    assert [page["content"] for page in pages] == [
        "First paragraph\nSecond line\nAnother paragraph",
        "",
    ]
    assert [page["page"] for page in pages] == [1, 2]
    assert [page["printed_page_label"] for page in pages] == ["i", "ii"]
