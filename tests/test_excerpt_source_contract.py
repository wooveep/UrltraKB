"""Readers preserve source qualification while generated prose has no copy contract."""

import asyncio
import json

import pytest
from compile_work_fixtures import accepted_payload
from reading_fixtures import publish_original
from test_answer_evidence_regression import agent_for, invoke
from test_native_table_projection import native_page
from test_source_qualification import freeze_source

from ultrakb.agent.document_summary import build_document_summary
from ultrakb.agent.evidence_payload import project_evidence, slice_evidence
from ultrakb.agent.source_excerpts import SourceExcerpts
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.compile_checkpoints import digest
from ultrakb.locks import atomic_write_text
from ultrakb.text_source import read_text_selection

pytest_plugins = ("compile_work_fixtures",)

LITERAL_PATH = "/var/&lt;target&gt;/state"
VISIBLE_PATH = "/var/<target>/state"
TABLE = f"<table><tr><td>{LITERAL_PATH}</td></tr></table>"


def validated_page_input(excerpts, path):
    from ultrakb.agent.page_delivery import validate_page_delivery

    value = json.dumps({"description": "Printed syntax", "content": f"Inspect `{path}`."})
    return json.loads(validate_page_delivery(value, original_excerpts=excerpts))


@pytest.mark.parametrize("format,qualified", [("pdf", False), ("md", True)])
def test_response_contract_agrees_in_import_and_question(kb_dir, tmp_path, format, qualified):
    import pymupdf
    from publication_fixtures import publish_document

    from ultrakb.config import LlmCredentialBundle

    path = tmp_path / f"manual.{format}"
    if format == "pdf":
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((60, 70), TABLE, fontsize=10)
            pdf.save(path)
    else:
        atomic_write_text(path, TABLE)
    before = path.read_bytes()
    expected_path = VISIBLE_PATH if qualified else LITERAL_PATH
    result = publish_document(
        kb_dir, path, f"Inspect `{expected_path}`.", bundle=LlmCredentialBundle()
    )
    assert result.status == "added", result.message
    from ultrakb.application.sources import source_inventory

    doc_name = source_inventory(kb_dir)[0]["units"][0]["doc_name"]
    reader = "get_text_content" if qualified else "get_page_content"
    arguments = {"chars": f"0:{len(TABLE)}"} if qualified else {"pages": "1"}
    delivered = asyncio.run(invoke(agent_for(kb_dir), reader, doc_name=doc_name, **arguments))
    public = json.loads(delivered.split("\n\n", 1)[1].split("\nSource context: ", 1)[0])
    assert len(public.get("native_tables", [])) == int(qualified)
    assert LITERAL_PATH in public["content"]
    assert expected_path in (kb_dir / f"wiki/summaries/{doc_name}.md").read_text()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "format,qualified", [("native", True), ("legacy", False), ("md", True), ("txt", False)]
)
def test_response_contract_agrees_in_segmented_compile_and_question(
    kb_dir, tmp_path, format, qualified
):
    from ultrakb.agent.compiler import _write_summary

    wiki = kb_dir / "wiki"
    if format in {"native", "legacy"}:
        source = wiki / "sources/manual.json"
        page = (
            native_page(TABLE) if qualified else {"page": 1, "unit_kind": "page", "content": TABLE}
        )
        atomic_write_text(source, json.dumps([page]))
        publish_original(kb_dir, [page])
        reader_name, arguments = "get_page_content", {"pages": "1"}
    else:
        _, frozen = freeze_source(kb_dir, tmp_path, format, TABLE)
        source = wiki / "sources/manual.content.json"
        reader_name, arguments = "get_text_content", {"chars": f"0:{len(frozen.text)}"}
    before = source.read_bytes()
    windows = CompileSourceWindows(wiki, "manual")
    window = next(windows.iter_document_windows(fits=lambda _: True))
    expected_path = VISIBLE_PATH if qualified else LITERAL_PATH
    result = validated_page_input(SourceExcerpts([window]), expected_path)
    _write_summary(
        wiki,
        "manual",
        result["content"],
        doc_type="pageindex",
        description=result["description"],
        full_text=str(windows.original_path),
    )
    assert expected_path in (wiki / "summaries/manual.md").read_text()
    delivered = asyncio.run(invoke(agent_for(kb_dir), reader_name, doc_name="manual", **arguments))
    payload = json.loads(delivered.split("\n\n", 1)[1].split("\nSource context: ", 1)[0])
    assert len(payload.get("native_tables", [])) == int(qualified)
    if qualified:
        cell = payload["native_tables"][0]["cells"][0]
        assert cell["value"] == VISIBLE_PATH and cell["value_coverage"] == "complete"
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    "body,partial,accepted",
    [
        (LITERAL_PATH, False, True),
        (f"<span>{LITERAL_PATH}</span>", False, True),
        (LITERAL_PATH, True, False),
        (f"<input value='42'>{LITERAL_PATH}", False, False),
        ("/var/&lt;tar<br>get&gt;/state", False, False),
        ("<p>/var/&lt;tar</p><p>get&gt;/state</p>", False, False),
    ],
)
def test_generated_paths_do_not_require_a_complete_resolved_cell(
    kb_dir, tmp_path, body, partial, accepted
):
    text = f"<table><tr><td>{body}</td></tr></table>"
    _, frozen = freeze_source(kb_dir, tmp_path, "md", text)
    selected = read_text_selection(frozen.model_dump(mode="json"))
    evidence = (
        slice_evidence(selected, 0, selected["content"].index("</td>"))
        if partial
        else project_evidence(selected)
    )
    excerpts = SourceExcerpts([evidence])
    response = f"Inspect `{VISIBLE_PATH}`."
    assert excerpts.render(response) == response


def test_unqualified_legacy_text_keeps_exact_excerpt_without_decoding():
    excerpts = SourceExcerpts([TABLE])
    assert VISIBLE_PATH in excerpts.render(f"Inspect `{VISIBLE_PATH}`.")
    assert TABLE in excerpts.render("[[original:x1.1]]")
    assert excerpts.render(f"Inspect `{LITERAL_PATH}`.") == f"Inspect `{LITERAL_PATH}`."


@pytest.mark.parametrize("qualified", [True, False])
@pytest.mark.parametrize("segmented", [True, False])
@pytest.mark.parametrize("kind", ["concepts", "entities"])
def test_new_pages_consume_the_same_response_proof(kb_dir, qualified, segmented, kind):
    from ultrakb.agent.compile_delivery import publish_compile_results
    from ultrakb.agent.compile_plan import normalize_plan
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.locks import atomic_write_json

    wiki = kb_dir / "wiki"
    source = wiki / "sources/manual.md"
    atomic_write_text(source, TABLE)
    page = native_page(TABLE) if qualified else {"page": 1, "unit_kind": "page", "content": TABLE}
    atomic_write_json(wiki / "sources/manual.json", [page])
    reader = CompileSourceWindows(wiki, "manual")
    originals = reader.prepare({"reading_locations": {"pages": "1"}}, long=True)
    excerpts = reader.original_excerpts(originals, long=True)
    assert VISIBLE_PATH in validated_page_input(excerpts, VISIBLE_PATH)["content"]
    expected_path = VISIBLE_PATH if qualified else LITERAL_PATH
    delivered = json.loads(
        validate_page_delivery(
            json.dumps(
                {"description": "Rule", "content": f"Read `{expected_path}`.", "type": "other"}
            ),
            original_excerpts=excerpts,
        )
    )
    item = {"name": "rules", "type": "other", "reading_locations": {"pages": "1"}}
    plan = normalize_plan(
        {kind: {"create": [item]}},
        existing={"concepts": set(), "entities": set()},
        entity_types=frozenset({"other"}),
        sanitize=lambda name: name,
    )
    result = (
        ("rules", delivered["content"], False, delivered["description"])
        if kind == "concepts"
        else ("rules", delivered["content"], delivered["description"], "other")
    )
    publish_compile_results(
        wiki,
        "manual",
        plan,
        summary="Printed syntax.",
        concept_results=[result] if kind == "concepts" else (),
        entity_results=[result] if kind == "entities" else (),
        rewrite_summary=True,
        doc_type="pageindex" if segmented else "short",
        original_path="sources/manual.json" if segmented else "sources/manual.md",
    )
    assert expected_path in (wiki / kind / "rules.md").read_text()
    assert source.read_text() == TABLE


@pytest.mark.parametrize("qualified", [True, False])
def test_saved_summary_response_uses_current_source_proof_without_regeneration(
    kb_dir, qualified, compile_work
):
    from ultrakb.agent.document_summary import summary_messages
    from ultrakb.compile_checkpoints import digest
    from ultrakb.locks import atomic_write_json

    source = kb_dir / "wiki/sources/manual.json"
    page = native_page(TABLE) if qualified else {"page": 1, "unit_kind": "page", "content": TABLE}
    atomic_write_json(source, [page])
    before = source.read_bytes()
    saved = {
        "description": "Saved",
        "sections": [
            {
                "id": "section-1",
                "parts": [{"kind": "prose", "text": f"Inspect `{VISIBLE_PATH}`.", "refs": []}],
            }
        ],
    }
    original_value = json.dumps(saved)
    reader = CompileSourceWindows(kb_dir / "wiki", "manual")
    system = {"role": "system", "content": "Compile"}
    window = next(reader.iter_document_windows(fits=lambda _: True))
    checkpoints = compile_work({"document": "manual"}, {"protocol": "summary-json"})
    checkpoints.save(
        "summary:1",
        digest(summary_messages(system, "manual", window, structured=True, request_format=3)),
        saved,
    )
    retained = accepted_payload(checkpoints, "summary:1")

    async def forbidden(*args):
        raise AssertionError("An accepted result must not regenerate")

    async def compile_saved():
        return await build_document_summary(
            reader,
            system_message=system,
            generate=forbidden,
            fits=lambda _: True,
            checkpoints=checkpoints,
        )

    assert VISIBLE_PATH in asyncio.run(compile_saved()).markdown
    assert json.dumps(saved) == original_value and source.read_bytes() == before
    assert accepted_payload(checkpoints, "summary:1") == retained


@pytest.mark.parametrize("qualified", [True, False])
def test_saved_short_summary_uses_the_same_proof_without_regeneration(
    kb_dir, qualified, compile_work
):
    from ultrakb.agent.compiler import _write_summary
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.locks import atomic_write_json

    wiki = kb_dir / "wiki"
    source = wiki / "sources/manual.md"
    atomic_write_text(source, TABLE)
    page = native_page(TABLE) if qualified else {"page": 1, "unit_kind": "page", "content": TABLE}
    atomic_write_json(wiki / "sources/manual.json", [page])
    reader = CompileSourceWindows(wiki, "manual")
    originals = reader.prepare({"reading_locations": {"pages": "1"}}, long=True)
    saved = json.dumps({"description": "Saved", "content": f"Inspect `{VISIBLE_PATH}`."})
    checkpoints = compile_work({"document": "manual"}, {"protocol": "summary-page-json"})
    checkpoints.save("short-summary", digest("same-request"), saved)
    retained = accepted_payload(checkpoints, "summary:1")
    received = json.loads(
        validate_page_delivery(
            checkpoints.load("short-summary", digest("same-request")),
            response_protocol="summary-page-json",
            original_excerpts=reader.original_excerpts(originals, long=True),
        )
    )
    _write_summary(wiki, "manual", received["content"], description=received["description"])
    assert VISIBLE_PATH in (wiki / "summaries/manual.md").read_text()
    assert source.read_text() == TABLE and accepted_payload(checkpoints, "summary:1") == retained


@pytest.mark.parametrize("qualified", [True, False])
@pytest.mark.parametrize("cached", [True, False])
def test_page_update_response_uses_the_same_cell_proof(kb_dir, qualified, cached, compile_work):
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_updates import PageUpdate
    from ultrakb.locks import atomic_write_json

    wiki = kb_dir / "wiki"
    source = wiki / "sources/manual.json"
    page = native_page(TABLE) if qualified else {"page": 1, "unit_kind": "page", "content": TABLE}
    atomic_write_json(source, [page])
    target = wiki / "concepts/rules.md"
    old = "Old recovery rule.\n"
    atomic_write_text(target, old)
    reader = CompileSourceWindows(wiki, "manual")
    originals = reader.prepare({"reading_locations": {"pages": "1"}}, long=True)
    patch = PageUpdate(old)
    excerpts = reader.original_excerpts(originals, long=True)
    value = {
        "base_sha256": patch.digest,
        "edits": [
            {"block_id": "b1", "operation": "replace", "content": f"Read `{VISIBLE_PATH}`."},
        ],
    }
    before = json.dumps(value)
    checkpoints = compile_work({"document": "manual"}, {"protocol": "page-edits"})
    if cached:
        checkpoints.save("page:batch:0", digest("same-request"), value)
        delivered = checkpoints.load("page:batch:0", digest("same-request"))
    else:
        delivered = value
    result = json.loads(
        validate_page_delivery(
            json.dumps(delivered),
            page_update=patch,
            read_current=target.read_text,
            original_excerpts=excerpts,
        )
    )
    assert VISIBLE_PATH in result["content"]
    assert target.read_text() == old and json.dumps(value) == before
