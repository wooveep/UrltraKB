"""Step two can publish queryable originals independently of knowledge compilation."""

import asyncio
import json

import pytest
from agents.tool_context import ToolContext

from openkb.agent.source_tools import source_tools
from openkb.application.documents import import_document
from openkb.state import HashRegistry
from tests.http_model_fixture import evidence_response


def invoke(tools, name, **arguments):
    tool = next(tool for tool in tools if tool.name == name)
    return json.loads(
        asyncio.run(
            tool.on_invoke_tool(
                ToolContext(
                    context=None, tool_name=name, tool_call_id="source-test", tool_arguments="{}"
                ),
                json.dumps(arguments),
            )
        )
    )


def test_empty_step_three_keeps_source_queryable_from_the_start_of_planning(
    kb_dir,
    tmp_path,
    model_service,
):
    path = tmp_path / "instructions.md"
    path.write_text("# Procedure\n\nUse a 37 second timeout.")
    visible_during_planning = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload.get("stage") == "planning":
            return ""
        return evidence_response(payload)

    def observe(event):
        if event.get("stage") == "compiling":
            tools, _ = source_tools(kb_dir)
            visible_during_planning.append(invoke(tools, "list_sources")["sources"])
            assert not HashRegistry(kb_dir / ".openkb/hashes.json").all_entries()

    model_service.respond = respond
    result = import_document(kb_dir, path, on_event=observe)
    assert visible_during_planning and all(visible_during_planning)
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    assert not list((kb_dir / "wiki/entities").glob("*.md"))
    tools, _ = source_tools(kb_dir)
    listed = invoke(tools, "list_sources")["sources"]
    assert listed[0]["source_id"] == result.source_id
    assert listed[0]["source_queryable"] is True
    found = invoke(tools, "search_source_text", source_id=result.source_id, query="37 second")
    assert any("37 second" in row["text"] for row in found["evidence"])


@pytest.mark.parametrize("mode", ["plan", "parse"])
def test_inspection_modes_keep_new_sources_private(kb_dir, tmp_path, model_service, mode):
    from openkb.application.source_actions import reparse_source
    from openkb.query_sources import query_source_bindings
    from tests.test_source_evidence import save_source

    path = tmp_path / "private.md"
    path.write_text("# Private\n\nPrivate original.")
    if mode == "plan":
        import_document(kb_dir, path, plan_only=True)
    else:
        source = save_source(kb_dir, path)
        reparse_source(kb_dir, source.source_id, version_id=source.id)
    assert not query_source_bindings(kb_dir)
    tools, _ = source_tools(kb_dir)
    assert invoke(tools, "list_sources")["sources"] == []
    assert not list((kb_dir / "wiki/sources/snapshots").glob("*.md"))


def test_query_only_replacement_freezes_old_reads_and_removal_withdraws_visibility(
    kb_dir,
    tmp_path,
    model_service,
    monkeypatch,
):
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.application.removal import remove_document
    from openkb.application.source_cleanup import preview_history_cleanup
    from openkb.application.source_history import source_status
    from openkb.processing import ProcessingIncomplete
    from openkb.query_sources import query_source_bindings

    def stop_before_generation(*args, **kwargs):
        raise ProcessingIncomplete("request_budget_exhausted", "planning")

    monkeypatch.setattr("openkb.agent.evidence_compiler.compile_evidence", stop_before_generation)
    path = tmp_path / "versions.md"
    path.write_text("# Timeout\n\nWait 37 seconds.")
    first = import_document(kb_dir, path)
    assert not HashRegistry(kb_dir / ".openkb/hashes.json").all_entries()
    old_tools, _ = source_tools(kb_dir)
    old = invoke(old_tools, "search_source_text", source_id=first.source_id, query="37 seconds")
    path.write_text("# Timeout\n\nWait 42 seconds.")
    second = import_document(kb_dir, path)
    assert first.source_id == second.source_id and first.input_version != second.input_version
    assert source_status(kb_dir, second.source_id)["source_queryable"]
    tools, _ = source_tools(kb_dir)
    assert invoke(tools, "search_source_text", source_id=first.source_id, query="42 seconds")[
        "evidence"
    ]
    assert invoke(old_tools, "search_source_text", source_id=first.source_id, query="37 seconds")[
        "evidence"
    ]
    from openkb.locks import atomic_write_text

    atomic_write_text(
        kb_dir / "wiki/concepts/citation.md", "# Retained\n" + old["evidence"][0]["citation"]
    )
    preview = preview_history_cleanup(kb_dir)
    assert (
        first.input_version not in preview.versions and second.input_version not in preview.versions
    )
    removed = remove_document(kb_dir, first.source_id)
    assert removed.status == "removed", removed
    assert not query_source_bindings(kb_dir)
    assert invoke(source_tools(kb_dir)[0], "list_sources")["sources"] == []
    assert get_kb_list(kb_dir)["documents"] == []
    assert invoke(old_tools, "search_source_text", source_id=first.source_id, query="37 seconds")[
        "evidence"
    ]
    assert first.input_version not in preview_history_cleanup(kb_dir).versions
    import_document(kb_dir, path)
    assert get_kb_list(kb_dir)["documents"][0]["source_queryable"]


def test_legacy_published_source_remains_queryable_without_new_table(
    kb_dir, tmp_path, model_service
):
    import sqlite3
    from contextlib import closing

    from openkb.locks import kb_ingest_lock

    path = tmp_path / "legacy.md"
    path.write_text("# Legacy\n\nRetain the valve position.")
    imported = import_document(kb_dir, path)
    with kb_ingest_lock(kb_dir / ".openkb"):
        with closing(sqlite3.connect(kb_dir / ".openkb/pageindex.db")) as connection, connection:
            connection.execute("DROP TABLE openkb_query_sources")
    tools, _ = source_tools(kb_dir)
    assert invoke(tools, "list_sources")["sources"][0]["source_id"] == imported.source_id
    assert invoke(tools, "search_source_text", source_id=imported.source_id, query="valve")[
        "evidence"
    ]


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
def test_remove_adapters_resolve_query_only_sources(
    kb_dir, tmp_path, model_service, monkeypatch, entrypoint
):
    from openkb.processing import ProcessingIncomplete

    def interrupt(*args, **kwargs):
        raise ProcessingIncomplete("request_budget_exhausted", "planning")

    monkeypatch.setattr("openkb.agent.evidence_compiler.compile_evidence", interrupt)
    path = tmp_path / "query-only.md"
    path.write_text("# Valve\n\nClose the valve first.")
    import_document(kb_dir, path)
    assert not HashRegistry(kb_dir / ".openkb/hashes.json").all_entries()
    if entrypoint == "api":
        from openkb.application.removal import run_remove_for_api

        result = run_remove_for_api(kb_dir, path.name)
        assert result["status"] == "removed"
    else:
        from click.testing import CliRunner

        from openkb.cli import cli

        result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "remove", path.name, "--yes"])
        assert result.exit_code == 0, result.output
        assert "removed from knowledge base" in result.output
    assert invoke(source_tools(kb_dir)[0], "list_sources")["sources"] == []


def test_activation_failure_keeps_old_binding_and_rolls_back_new_snapshot(
    kb_dir, tmp_path, model_service, monkeypatch
):
    from openkb.query_sources import query_source_bindings

    path = tmp_path / "atomic.md"
    path.write_text("# Timeout\n\nWait 37 seconds.")
    first = import_document(kb_dir, path)
    before = query_source_bindings(kb_dir)
    snapshots = kb_dir / "wiki/sources/snapshots"
    prior_resources = {p.name: p.read_bytes() for p in snapshots.iterdir()}

    def fail_binding(*args, **kwargs):
        raise OSError("Could not commit query binding")

    monkeypatch.setattr(
        "openkb.application.source_query_publication.bind_query_source", fail_binding
    )
    path.write_text("# Timeout\n\nWait 42 seconds.")
    failed = import_document(kb_dir, path)
    assert failed.status == "failed" and "OSError" in failed.reason
    assert query_source_bindings(kb_dir) == before
    assert {p.name: p.read_bytes() for p in snapshots.iterdir()} == prior_resources
    assert invoke(
        source_tools(kb_dir)[0], "search_source_text", source_id=first.source_id, query="37 seconds"
    )["evidence"]


def test_cached_compilation_reactivates_its_parse_after_a_private_reparse(
    kb_dir, tmp_path, model_service, monkeypatch
):
    from openkb.evidence import BlockDraft, ParseStore
    from openkb.processing import ProcessingIncomplete
    from openkb.query_sources import query_source_bindings
    from openkb.sources import SourceStore

    path = tmp_path / "parse-views.md"
    path.write_text("# Setting\n\nWait 37 seconds.")
    first = import_document(kb_dir, path)
    source = SourceStore(kb_dir).current(first.source_id)
    parses = ParseStore(kb_dir)
    old_parse = parses.load(first.parse_id)
    changed_parse = parses.save(
        source,
        {"parser": "alternate-ocr"},
        [
            BlockDraft("# Setting", "heading", {"kind": "text", "headings": ["Setting"]}),
            BlockDraft("Wait 42 seconds.", "paragraph", {"kind": "text", "headings": ["Setting"]}),
        ],
    )
    frozen, _ = source_tools(kb_dir)
    assert query_source_bindings(kb_dir)[first.source_id]["parse_id"] == old_parse.id

    def interrupt(*args, **kwargs):
        raise ProcessingIncomplete("request_budget_exhausted", "planning")

    with monkeypatch.context() as patch:
        patch.setattr(
            "openkb.application.document_pipeline.parse_document", lambda *a, **k: changed_parse
        )
        patch.setattr("openkb.agent.evidence_compiler.compile_evidence", interrupt)
        import_document(kb_dir, path)
    assert query_source_bindings(kb_dir)[first.source_id]["parse_id"] == changed_parse.id
    assert invoke(frozen, "search_source_text", source_id=first.source_id, query="37 seconds")[
        "evidence"
    ]
    model_service.clear()
    monkeypatch.setattr(
        "openkb.application.document_pipeline.parse_document", lambda *a, **k: old_parse
    )
    cached = import_document(kb_dir, path)
    assert cached.status == "skipped" and not model_service
    assert query_source_bindings(kb_dir)[first.source_id]["parse_id"] == old_parse.id
