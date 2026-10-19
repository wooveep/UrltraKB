"""Physical structure needs extraction evidence, including across consumers."""

import json

import pymupdf
import pytest
from native_source_fixtures import LITERAL_TABLE
from pageindex import IndexConfig
from test_answer_evidence_regression import agent_for, invoke
from test_index_pdf_layout import RecordingPdfIndex

from ultrakb.agent.evidence_payload import project_evidence, slice_evidence
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.images import convert_pdf_to_pages, convert_pdf_with_images
from ultrakb.index_client import create_index_client
from ultrakb.locks import atomic_write_json
from ultrakb.source_contract import NATIVE_PAGE_POLICY
from ultrakb.source_pages import read_page_selection


def table_pdf(path, *, literal, grid):
    with pymupdf.open() as document:
        page = document.new_page(width=900, height=500)
        page.insert_text((50, 50), "Native source text.")
        if literal:
            page.insert_text((50, 80), LITERAL_TABLE, fontsize=9)
        if grid:
            for x in (50, 190, 330):
                page.draw_line((x, 140), (x, 220))
            for y in (140, 180, 220):
                page.draw_line((50, y), (330, y))
            for x, y, text in (
                (60, 165, "Object"),
                (200, 165, "Value"),
                (60, 205, "Native"),
                (200, 205, "42"),
            ):
                page.insert_text((x, y), text)
        document.save(path)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("literal,grid", [(True, False), (False, True), (True, True)])
async def test_pdf_table_authority_agrees_in_short_index_compile_and_question(
    kb_dir, tmp_path, literal, grid, live_llm, monkeypatch
):
    live_llm.install(monkeypatch)
    pdf = table_pdf(tmp_path / "manual.pdf", literal=literal, grid=grid)
    original = pdf.read_bytes()
    with pymupdf.open(pdf) as document:
        assert len(document[0].find_tables().tables) == int(grid)
    pages = convert_pdf_to_pages(pdf, "manual", tmp_path / "images")
    short_pages = []
    convert_pdf_with_images(pdf, "manual", tmp_path / "short-images", page_records=short_pages)
    options = IndexConfig(
        llm_client=RecordingPdfIndex(live_llm=live_llm),
        if_add_node_summary=False,
        if_add_doc_description=False,
    )
    with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
        collection = client.collection()
        identity = collection.add(str(pdf))
        indexed = collection.get_page_content(identity, "1")
    atomic_write_json(kb_dir / "wiki/sources/manual.json", pages)
    compiled = CompileSourceWindows(kb_dir / "wiki", "manual").read_selection(
        "sources/manual.json", {"pages": "1"}
    )[0]
    response = await invoke(agent_for(kb_dir), "get_page_content", doc_name="manual", pages="1")
    public = json.loads(response.split("\n\n", 1)[1].split("\nSource context: ", 1)[0])
    payloads = [
        project_evidence(read_page_selection(items)) for items in (pages, short_pages, indexed)
    ]
    for payload in [*payloads, compiled, public]:
        tables = payload.get("native_tables", [])
        assert len(tables) == int(grid)
        if literal:
            assert LITERAL_TABLE in payload["content"]
        assert not any(c.get("value") == "Printed example" for t in tables for c in t["cells"])
        if grid:
            assert [c["value"] for c in tables[0]["cells"]] == ["Object", "Value", "Native", "42"]
    assert pdf.read_bytes() == original


@pytest.mark.asyncio
async def test_bounded_pdf_read_and_gallery_use_native_image_occurrence(kb_dir, tmp_path):
    from figure_fixtures import png_bytes

    path = "sources/images/manual/p1_img1.png"
    caption = "PRINTED_SYNTAX_EXAMPLE"
    literal = f"![{caption}]({path})"
    pdf = tmp_path / "manual.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=900, height=18000)
        page.insert_text((50, 50), "Printed syntax example:")
        page.insert_text((50, 80), literal, fontsize=9)
        for i in range(800):
            y = 120 + i * 20
            page.insert_text((50, y), f"# Printed filler {i}", fontsize=7)
            page.insert_text(
                (50, y + 9), "Recorded body text for bounded reading. " * 2, fontsize=7
            )
        page.insert_image(pymupdf.Rect(50, 17000, 150, 17100), stream=png_bytes())
        page.insert_text((50, 17200), "Actual native image appears above.")
        document.save(pdf)
    pages = convert_pdf_to_pages(pdf, "manual", kb_dir / "wiki/sources/images/manual")
    source_path = kb_dir / "wiki/sources/manual.json"
    atomic_write_json(source_path, pages)
    from reading_fixtures import publish_original

    publish_original(kb_dir, pages)
    original = source_path.read_bytes()
    selected = read_page_selection(pages)
    left = selected["_page_segments"][0][0]
    actual_start, actual_end = pages[0]["image_reference_spans"][0]
    printed_start = pages[0]["content"].index(literal)
    printed = slice_evidence(selected, left + printed_start, left + printed_start + len(literal))
    actual = slice_evidence(selected, left + actual_start, left + actual_end)
    assert not printed["units"][0]["images"]
    assert actual["units"][0]["images"] == pages[0]["images"]
    response = await invoke(agent_for(kb_dir), "get_page_content", doc_name="manual", pages="1")
    public = json.loads(response.split("\n\n", 1)[1].split("\nSource context: ", 1)[0])
    assert actual_start > max(span["end"] for span in public["page_spans"])
    assert not public["units"][0]["images"]
    coverage = json.loads(response.split("\nImage coverage: ", 1)[1])
    assert not coverage["occurrences"]
    windows = CompileSourceWindows(kb_dir / "wiki", "manual")
    projected = windows.page_figures([{"path": "sources/manual.json", **printed}], long=True)
    assert not projected.figures
    figures = windows.page_figures()
    assert len(figures.figures) == 1
    assert figures.figures[0].caption == ""
    rendered = figures.decorate(
        "summaries/manual.md", {"content": "Observed rule.", "source_ranges": list(figures.ids)}
    )
    assert caption not in rendered["content"]
    assert "figure-assets/" in rendered["content"]
    assert source_path.read_bytes() == original


def test_reused_asset_is_selected_by_occurrence_instead_of_path():
    path = "sources/images/shared.png"
    text = f"First\n![image]({path})\n\nSecond\n![image]({path})\n"
    first = text.index(path)
    second = text.rindex(path)
    spans = [[first, first + len(path)], [second, second + len(path)]]
    page = {
        "page": 1,
        "unit_kind": "page",
        "content": text,
        "source_structure_policy": NATIVE_PAGE_POLICY,
        "content_spans": [{"role": "image", "span": span} for span in spans],
        "image_reference_spans": spans,
        "images": [
            {"path": path, "caption": "Original A"},
            {"path": path, "caption": "Original B"},
        ],
    }
    selected = read_page_selection([page])
    left = selected["_page_segments"][0][0]
    excerpt = slice_evidence(selected, left + second, left + second + len(path))
    assert excerpt["units"][0]["images"] == [{"path": path, "caption": "Original B"}]
    assert excerpt["units"][0]["image_ordinals"] == [1]


def test_relocating_an_image_before_a_table_preserves_native_coordinates(tmp_path):
    from copy import deepcopy

    from figure_fixtures import png_bytes
    from pageindex.backend.input_package import freeze_pages, materialize_pages

    from ultrakb.index_pdf import NativePdfParser

    pdf = table_pdf(tmp_path / "manual.pdf", literal=True, grid=True)
    with pymupdf.open(pdf) as document:
        document[0].insert_image(pymupdf.Rect(50, 95, 100, 130), stream=png_bytes())
        document.saveIncr()
    base = tmp_path / "managed"
    parsed = NativePdfParser().parse(str(pdf), images_dir=str(base / "images"))
    assert parsed.nodes[0].content.index("![image]") < parsed.nodes[0].content.rindex("<table>")
    cached = freeze_pages(parsed, base, None)
    original = json.dumps(cached)
    restored = materialize_pages(
        cached, base, {**parsed.metadata, "parser_policy": NativePdfParser.policy}
    )
    for pages in [cached, restored]:
        table = project_evidence(read_page_selection(pages))["native_tables"][0]
        assert [c["value"] for c in table["cells"]] == ["Object", "Value", "Native", "42"]
        span = table["native_location"]["page_spans"][0]
        assert pages[0]["content"][span["start"] : span["end"]].startswith("<table>")
        assert LITERAL_TABLE in pages[0]["content"]
    assert json.dumps(cached) == original
    corrupted = deepcopy(cached)
    next(s for s in corrupted[0]["content_spans"] if s["role"] == "table")["span"][0] += 1
    with pytest.raises(ValueError, match="reference mapping changed"):
        materialize_pages(corrupted, base, parsed.metadata)
