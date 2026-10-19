"""Read old frozen records and paid index caches without migrating their inputs."""

import base64
import hashlib
import json
from pathlib import Path

import pytest
from native_source_fixtures import LITERAL_TABLE
from pageindex import IndexConfig
from test_index_llm import RecordingIndexLLM

from ultrakb.agent.evidence_payload import project_evidence
from ultrakb.condb_storage import ConDBPageIndexStorage
from ultrakb.index_client import create_index_client
from ultrakb.index_pdf import NativePdfParser
from ultrakb.locks import atomic_write_bytes, atomic_write_json, atomic_write_text
from ultrakb.office.slide_package import FrozenSlideParser
from ultrakb.source_pages import read_page_selection, select_page_part


def _legacy_cache_documents(tmp_path):
    import pymupdf
    from test_slide_index_contract import slide_package

    from ultrakb.office.slide_package import SlidePackage

    notes = "Native speaker note.\n" + LITERAL_TABLE
    path = slide_package(
        tmp_path,
        ["1. Calibration\nApply the cold-only valve setting."],
        [notes],
        cover_image=True,
    )
    package = SlidePackage.model_validate_json(path.read_text())
    with pymupdf.open(stream=base64.b64decode(package.pdf), filetype="pdf") as document:
        page = document[0]
        page.draw_rect(pymupdf.Rect(72, 200, 300, 252))
        page.draw_line((186, 200), (186, 252))
        page.draw_line((72, 226), (300, 226))
        for x, y, text in (
            (78, 217, "Rule"),
            (192, 217, "Mode"),
            (78, 243, "Valve"),
            (192, 243, "cold-only"),
        ):
            page.insert_text((x, y), text, fontsize=9)
        body = page.get_text()
        data = document.tobytes()
    pdf = tmp_path / "manual.pdf"
    atomic_write_bytes(pdf, data)
    package = package.model_copy(
        update={
            "pdf": base64.b64encode(data).decode("ascii"),
            "pdf_digest": hashlib.sha256(data).hexdigest(),
            "slides": [package.slides[0].model_copy(update={"body": body})],
        }
    )
    atomic_write_text(path, package.model_dump_json())
    return pdf, path, notes


def test_legacy_cache_input_keeps_distinct_parser_roles_notes_and_images(tmp_path):
    from ultrakb.office.slide_content import LEGACY_NOTES_POLICY

    _, package, notes = _legacy_cache_documents(tmp_path)
    original = package.read_bytes()
    legacy = FrozenSlideParser(legacy=True).parse(
        str(package), images_dir=str(tmp_path / "legacy-images")
    )
    native = FrozenSlideParser().parse(str(package), images_dir=str(tmp_path / "native-images"))
    assert len(legacy.nodes) == len(native.nodes) == 1
    assert "<table>" not in legacy.nodes[0].metadata["parts"]["body"]
    assert "<table>" in native.nodes[0].metadata["parts"]["body"]
    assert legacy.metadata["notes_policy"] == LEGACY_NOTES_POLICY
    for parsed in (legacy, native):
        assert parsed.nodes[0].metadata["parts"]["notes"] == notes
        assert parsed.nodes[0].images
        assert all(Path(image["path"]).is_file() for image in parsed.nodes[0].images)
    assert package.read_bytes() == original


@pytest.mark.parametrize("kind", ["pdf", "slides"])
def test_missing_legacy_cache_uses_its_recorded_parser_without_model_calls(
    tmp_path, monkeypatch, live_llm, kind
):
    pdf, package, notes = _legacy_cache_documents(tmp_path)
    path = package if kind == "slides" else pdf
    original = path.read_bytes()
    parser = FrozenSlideParser(legacy=True) if kind == "slides" else NativePdfParser(legacy=True)
    live_llm.install(monkeypatch)
    runtime = RecordingIndexLLM(live_llm=live_llm)
    options = IndexConfig(
        model="deepseek/deepseek-flash",
        llm_client=runtime,
        require_llm_client=True,
        if_add_node_summary=False,
        if_add_doc_description=False,
    )
    with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
        client.register_parser(parser)
        collection = client.collection()
        identity = collection.add(str(path))
        before = collection.get_page_content(identity, "1")
    requests = len(runtime.calls)
    stages = {stage for stage, _ in runtime.calls}
    assert "structure" in stages and stages <= {"structure", "verification"}
    assert before[0]["images"]
    assert all(Path(image["path"]).is_file() for image in before[0]["images"])
    if kind == "slides":
        assert "<table>" not in before[0]["parts"]["body"]
        assert before[0]["parts"]["notes"] == notes
    monkeypatch.setattr(ConDBPageIndexStorage, "get_pages", lambda *args: None)
    no_generation = RecordingIndexLLM()
    recovery = options.model_copy(update={"llm_client": no_generation})
    with create_index_client(storage_path=tmp_path / "index", index_config=recovery) as client:
        after = client.collection().get_page_content(identity, "1")
    assert after == before
    assert len(runtime.calls) == requests
    assert no_generation.calls == []
    assert path.read_bytes() == original


def test_previous_generated_image_spans_remain_readable_in_original_domain():
    path = "sources/images/old.png"
    body = f"![image]({path})"
    start = body.index(path)
    page = {
        "page": 1,
        "unit_kind": "page",
        "content": body + "\nNotes",
        "images": [{"path": path}],
        "image_reference_spans": [[start, start + len(path)]],
        "parts": {"body": "Old plain body", "notes": "Notes"},
    }
    selected = read_page_selection([page])
    assert project_evidence(selected)["units"][0]["images"] == page["images"]
    notes = project_evidence(select_page_part(selected, "notes"))
    assert not notes["units"][0]["images"]
    # A pre-contract body has different coordinates; retain it as an unproved domain.
    old_body = project_evidence(select_page_part(selected, "body"))
    assert "Old plain body" in old_body["content"]
    assert old_body["units"][0]["image_position_coverage"] == "unconfirmed"


def test_reading_requalifies_caption_without_rewriting_old_figure_cache(kb_dir):
    from figure_fixtures import png_bytes

    from ultrakb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    path = "sources/images/legacy.png"
    (wiki / path).write_bytes(png_bytes())
    raw = [
        {
            "page": 1,
            "unit_kind": "page",
            "content": f"![Printed example]({path})",
            "images": [{"path": path}],
        }
    ]
    atomic_write_json(wiki / "sources/legacy.json", raw)
    CompileSourceWindows(wiki, "legacy").page_figures()
    cache = next((wiki / "sources/figure-records").glob("*.json"))
    older = json.loads(cache.read_text())
    older[0]["caption"] = "Printed example"
    atomic_write_json(cache, older)
    before = cache.read_bytes()
    figures = CompileSourceWindows(wiki, "legacy").page_figures()
    assert figures.figures[0].caption == ""
    assert cache.read_bytes() == before


def test_previous_html_policy_keeps_its_native_heading_proof(kb_dir, tmp_path):
    from test_source_qualification import freeze_source

    from ultrakb.html_source import LEGACY_HTML_POLICY
    from ultrakb.source_records import ENCODING_POLICY
    from ultrakb.text_source import read_text_selection

    _, current = freeze_source(kb_dir, tmp_path, "html", "<h1>Original heading</h1><p>Body.</p>")
    previous = current.model_copy(
        update={"normalization_policy": f"{LEGACY_HTML_POLICY}:{ENCODING_POLICY}"}
    )
    raw = previous.model_dump(mode="json")
    before = json.dumps(raw)
    selected = read_text_selection(raw)
    assert selected["_qualified_headings"][0]["title"] == "Original heading"
    assert json.dumps(raw) == before
