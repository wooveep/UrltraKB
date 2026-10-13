"""Read old frozen records and paid index caches without migrating their inputs."""

import json

import pytest
from pageindex import IndexConfig
from test_answer_evidence_regression import agent_for, invoke
from test_index_pdf_layout import TableIndexModel
from test_native_slide_contract import SlideModel, slide_fixture
from test_native_source_contract import LITERAL_TABLE

from openkb.agent.evidence_payload import project_evidence, slice_evidence
from openkb.condb_storage import ConDBPageIndexStorage
from openkb.index_client import create_index_client
from openkb.index_pdf import NativePdfParser
from openkb.locks import atomic_write_bytes, atomic_write_json, atomic_write_text
from openkb.office.slide_package import FrozenSlideParser
from openkb.source_pages import read_page_selection, select_page_part


@pytest.mark.parametrize("kind", ["pdf", "slides"])
def test_missing_legacy_cache_uses_its_recorded_parser_without_model_calls(
    tmp_path, monkeypatch, kind
):
    pdf, package, _ = slide_fixture(tmp_path)
    path = package if kind == "slides" else pdf
    original = path.read_bytes()
    parser = FrozenSlideParser(legacy=True) if kind == "slides" else NativePdfParser(legacy=True)
    runtime = SlideModel() if kind == "slides" else TableIndexModel()
    options = IndexConfig(
        llm_client=runtime, if_add_node_summary=False, if_add_doc_description=False
    )
    with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
        client.register_parser(parser)
        collection = client.collection()
        identity = collection.add(str(path))
        before = collection.get_page_content(identity, "1")
    requests = len(runtime.inputs if kind == "slides" else runtime.structure_inputs)
    if kind == "slides":
        assert "<table>" not in before[0]["parts"]["body"]
    monkeypatch.setattr(ConDBPageIndexStorage, "get_pages", lambda *args: None)
    with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
        after = client.collection().get_page_content(identity, "1")
    assert after == before
    assert len(runtime.inputs if kind == "slides" else runtime.structure_inputs) == requests
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_unproved_legacy_page_preserves_text_and_asset_availability(kb_dir):
    path = "sources/images/legacy.png"
    literal = f"![Printed caption]({path})"
    page = {
        "page": 1,
        "unit_kind": "page",
        "content": LITERAL_TABLE + "\n" + literal,
        "images": [{"path": path}],
    }
    target = kb_dir / "wiki/sources/legacy.json"
    atomic_write_json(target, [page])
    original = target.read_bytes()
    selected = read_page_selection([page])
    whole = project_evidence(selected)
    assert LITERAL_TABLE in whole["content"]
    assert "native_tables" not in whole
    assert whole["units"][0]["images"] == page["images"]
    assert whole["units"][0]["image_position_coverage"] == "unconfirmed"
    start = selected["content"].index(literal)
    excerpt = slice_evidence(selected, start, len(selected["content"]))
    assert not excerpt["units"][0]["images"]
    response = await invoke(agent_for(kb_dir), "get_page_content", doc_name="legacy", pages="1")
    public = json.loads(response.split("\n\n", 1)[1].split("\nSource context: ", 1)[0])
    assert public["content"] == whole["content"]
    assert "native_tables" not in public
    assert target.read_bytes() == original


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

    from openkb.agent.source_windows import CompileSourceWindows

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

    from openkb.html_source import LEGACY_HTML_POLICY
    from openkb.source_records import ENCODING_POLICY
    from openkb.text_source import read_text_selection

    _, current = freeze_source(kb_dir, tmp_path, "html", "<h1>Original heading</h1><p>Body.</p>")
    previous = current.model_copy(
        update={"normalization_policy": f"{LEGACY_HTML_POLICY}:{ENCODING_POLICY}"}
    )
    raw = previous.model_dump(mode="json")
    before = json.dumps(raw)
    selected = read_text_selection(raw)
    assert selected["_qualified_headings"][0]["title"] == "Original heading"
    assert json.dumps(raw) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "literal",
    ["![Printed example](images/legacy.png)", "![Printed example](data:image/png;base64,AA==)"],
)
async def test_bare_legacy_text_cannot_mint_figure_roles_or_demand_reprocessing(kb_dir, literal):
    from figure_fixtures import png_bytes

    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.figure_assets import freeze_figures

    wiki = kb_dir / "wiki"
    asset = wiki / "sources/images/legacy.png"
    atomic_write_bytes(asset, png_bytes())
    path = "sources/legacy.md"
    text = "Printed syntax example:\n" + literal + "\n"
    atomic_write_text(wiki / path, text)
    before = {path: (wiki / path).read_bytes(), "asset": asset.read_bytes()}
    assert freeze_figures(wiki, path, text, {}) == []
    windows = CompileSourceWindows(wiki, "legacy")
    assert not windows.page_figures().figures
    (compiled,) = windows.read_selection(path, {"chars": f"0:{len(text)}"})
    assert compiled["content"] == text and "figure_interpretation" not in compiled
    public = await invoke(agent_for(kb_dir), "read_file", path=path)
    assert literal in public and "figure_interpretation" not in public
    assert (wiki / path).read_bytes() == before[path]
    assert asset.read_bytes() == before["asset"]
    assert not (wiki / "sources/figure-records").exists()


def test_bare_legacy_compilation_keeps_accepted_checkpoint_and_page_base(kb_dir):
    import hashlib
    from types import SimpleNamespace

    from figure_fixtures import png_bytes

    from openkb.compile_checkpoints import CompileCheckpoints
    from openkb.locks import kb_ingest_lock
    from openkb.unit_publication import prepare_compile_view

    wiki = kb_dir / "wiki"
    atomic_write_bytes(wiki / "sources/images/legacy.png", png_bytes())
    atomic_write_text(wiki / "sources/legacy.md", "![Printed example](images/legacy.png)\n")
    page = "concepts/legacy.md"
    body = "---\nsources: [sources/legacy.md]\n---\nRetained rule. See `legacy.png`.\n"
    atomic_write_text(wiki / page, body)
    before = (wiki / page).read_bytes()
    checkpoint = CompileCheckpoints(
        kb_dir,
        SimpleNamespace(task_id="b" * 32, parent_task_id=None),
        {"document": "legacy"},
        {"protocol": "retained"},
    )
    checkpoint.bind_page(page, hashlib.sha256(before).hexdigest())
    checkpoint.save("summary:1", "retained-request", {"content": "Retained draft"})
    cached = checkpoint.path.read_bytes()
    with kb_ingest_lock(kb_dir / ".openkb"), prepare_compile_view(kb_dir) as view:
        assert (view.scope.wiki_dir / page).read_bytes() == before
        assert checkpoint.load("summary:1", "retained-request") == {"content": "Retained draft"}
        assert checkpoint.path.read_bytes() == cached
        assert not (view.scope.wiki_dir / "sources/figure-records").exists()
    assert (wiki / page).read_bytes() == before
    assert checkpoint.path.read_bytes() == cached
