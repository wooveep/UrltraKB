"""Exercise the actual SDK tool schemas and frozen evidence readers."""

import json

import pytest
from reading_fixtures import publish_original

from ultrakb.application.query_views import QuerySelection, resolve_query_views
from ultrakb.desktop.verification_data import publish_pages, retain_document
from ultrakb.file_digest import hash_file


def selection(kb_dir, versions=()):
    return resolve_query_views(kb_dir)


def agent_for(kb_dir):
    from agents import Agent

    from ultrakb.agent.query_evidence import restrict_query_agent

    return restrict_query_agent(Agent(name="fixture", instructions=""), selection(kb_dir))


async def invoke(agent, name, **arguments):
    from agents.tool_context import ToolContext

    tool = next(tool for tool in agent.tools if tool.name == name)
    payload = json.dumps(arguments)
    return await tool.on_invoke_tool(
        ToolContext(context=None, tool_name=name, tool_call_id="fixture", tool_arguments=payload),
        payload,
    )


@pytest.mark.asyncio
async def test_file_activity_observes_metadata_without_parsing_original_json(kb_dir):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.locks import kb_ingest_lock

    original = '{"status":"error","hits":[7]}'
    retain_document(kb_dir, "note.md", "note", original)
    events = []
    context = ExecutionContext(on_event=events.append)
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        result = await invoke(agent_for(kb_dir), "read_file", path="sources/note")
    assert result.endswith(original)
    completed = [
        e["data"]
        for e in events
        if e.get("event") == "activity" and e["data"]["stage"] == "reading"
    ][-1]
    assert completed["status"] == "completed"
    assert completed["source_count"] == completed["unit_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["部署条件", "Capacity", "operating conditions"])
async def test_sdk_reads_same_directory_markdown_alias(kb_dir, name):
    publish_pages(kb_dir, {f"concepts/{name}.md": b"A known operating condition."})
    agent = agent_for(kb_dir)
    result = await invoke(agent, "read_file", path=f"concepts/{name}")
    assert "A known operating condition." in result
    assert '"content_kind": "derived_concept"' in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "concepts/missing",
        "entities/Capacity",
        "concepts/capacity",
        "/concepts/Capacity",
        "concepts/../concepts/Capacity",
        "../wiki/concepts/Capacity",
    ],
)
async def test_sdk_markdown_alias_does_not_guess_or_escape(kb_dir, path):
    (kb_dir / "wiki/concepts/Capacity.md").write_text("Private operating condition.")
    result = json.loads(await invoke(agent_for(kb_dir), "read_file", path=path))
    assert result["status"] == "error"
    assert result["reading_action"]["arguments"] == {"path": "index.md"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["bytes", "symlink", "missing"])
async def test_sdk_alias_keeps_committed_bytes_after_working_copy_changes(kb_dir, change):
    page = kb_dir / "wiki/concepts/Capacity.md"
    publish_pages(kb_dir, {"concepts/Capacity.md": b"Selected fact."})
    agent = agent_for(kb_dir)
    if change == "bytes":
        page.write_text("Changed fact.")
    else:
        page.unlink()
        if change == "symlink":
            outside = kb_dir / "outside.md"
            outside.write_text("Private fact.")
            page.symlink_to(outside)
    result = await invoke(agent, "read_file", path="concepts/Capacity")
    assert "Selected fact." in result
    assert "Changed fact." not in result and "Private fact." not in result


def source(kb_dir, *, slides=False, images=()):
    parts = {"body": "Visible body", "notes": "Speaker notes"}
    data = [
        {
            "page": 1,
            "unit_kind": "page",
            "content": "Visible body\nSpeaker notes"
            if slides
            else "Compute nodes use management VIP after joining the cluster.",
            "images": [{"path": image} for image in images],
            **({"parts": parts} if slides else {}),
        }
    ]
    publish_original(kb_dir, data)


def test_bound_reader_schemas_only_accept_business_arguments(kb_dir):
    expected = {
        "search_knowledge": {"query"},
        "read_file": {"path"},
        "get_page_content": {"doc_name", "pages", "offset"},
        "get_text_content": {"doc_name", "chars", "offset"},
        "get_block_content": {"doc_name", "blocks", "offset"},
        "get_cell_content": {"doc_name", "cells", "offset"},
        "get_image": {"image_path"},
        "search_originals": {"terms", "doc_name"},
    }
    for tool in agent_for(kb_dir).tools:
        assert set(tool.params_json_schema["properties"]) == expected[tool.name]


@pytest.mark.asyncio
async def test_search_distinguishes_unknown_document_from_no_phrase_match(kb_dir):
    source(kb_dir)
    agent = agent_for(kb_dir)
    empty = await invoke(agent, "search_originals", terms=["unmentioned"], doc_name="manual")
    assert json.loads(empty)["status"] == "no_matches"
    unknown = await invoke(agent, "search_originals", terms=["VIP"], doc_name="missing")
    assert "Document not found" in unknown
    assert "index.md" in unknown


@pytest.mark.asyncio
async def test_first_catalog_read_uses_the_frozen_source_bytes(kb_dir):
    source(kb_dir)
    agent = agent_for(kb_dir)
    catalog = await invoke(agent, "read_file", path="index.md")
    assert '"doc_name": "manual"' in catalog
    assert '"pages": "1-1"' in catalog
    (kb_dir / "wiki/sources/manual.json").write_text("[]")
    changed = await invoke(agent, "read_file", path="index.md")
    assert changed == catalog


def test_application_rejects_ambiguous_reading_collections(kb_dir):
    view = selection(kb_dir).views[0]
    with pytest.raises(ValueError, match="Multiple source collections"):
        QuerySelection(kb_dir, (view, view))


@pytest.mark.asyncio
async def test_ordinary_pages_use_native_selector_and_continuation(kb_dir):
    source(kb_dir)
    agent = agent_for(kb_dir)
    tool = next(tool for tool in agent.tools if tool.name == "get_page_content")
    assert set(tool.params_json_schema["required"]) == {"doc_name", "pages", "offset"}
    assert "Physical page 1" in await invoke(agent, tool.name, doc_name="manual", pages="1")


@pytest.mark.asyncio
async def test_selected_page_returns_image_location_without_an_image_tool(kb_dir):
    import pymupdf

    path = "sources/diagram.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
    pix.clear_with(128)
    (kb_dir / "wiki" / path).write_bytes(pix.tobytes("png"))
    source(kb_dir, images=[path])
    agent = agent_for(kb_dir)
    result = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    assert isinstance(result, str)
    assert path in result and '"interpreted": false' in result
    assert "data:image" not in result
    assert "get_image" not in {tool.name for tool in agent.tools}


@pytest.mark.asyncio
async def test_image_changes_have_actionable_feedback_in_the_storage_reader(kb_dir):
    from ultrakb.agent.tools import read_wiki_image

    path = "sources/images/diagram.png"
    image = kb_dir / "wiki" / path
    image.write_bytes(b"original")
    digest = hash_file(image)
    image.write_bytes(b"changed")
    result = read_wiki_image(path, str(kb_dir / "wiki"), expected_digest=digest)
    assert result["type"] == "text"
    assert "changed after selection" in result["text"]
    image.unlink()
    missing = read_wiki_image(path, str(kb_dir / "wiki"), expected_digest=digest)
    assert missing["type"] == "text"
    assert "not found" in missing["text"]


@pytest.mark.asyncio
async def test_derived_page_read_points_to_original_without_certifying_its_claims(kb_dir):
    source(kb_dir)
    (kb_dir / "wiki/summaries/manual.md").write_text(
        "---\ntype: Summary\nfull_text: sources/manual.json\n---\nGenerated summary"
    )
    (kb_dir / "wiki/concepts/rules.md").write_text(
        "---\ntype: Concept\nsources: [summaries/manual.md]\n---\nUnsupported guarantee"
    )
    publish_pages(
        kb_dir,
        {
            path: (kb_dir / "wiki" / path).read_bytes()
            for path in ("summaries/manual.md", "concepts/rules.md")
        },
    )
    result = await invoke(agent_for(kb_dir), "read_file", path="concepts/rules.md")
    assert '"content_kind": "derived_concept"' in result
    assert '"doc_name": "manual"' in result
    assert '"reader": "get_page_content"' in result
    assert "not factual verification" in result


def test_answer_outcome_survives_runtime_receipt_readback():
    from ultrakb.runtime.records import UnitResult

    result = UnitResult("completed", answer_outcome="insufficient_evidence")
    # JSON is the actual persisted receipt boundary.
    assert UnitResult.from_summary(json.loads(json.dumps(result.summary()))) == result
