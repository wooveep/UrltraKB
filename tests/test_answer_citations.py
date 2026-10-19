"""Capture returned source versions; no focus or tool-log inference of citations."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest
from processing_fixtures import publish_processed_source

from ultrakb.application.answer_citations import (
    answer_citations,
    capture_answer_citations,
    citation_targets,
    read_answer_reference,
)
from ultrakb.application.answer_records import CompletedAnswer
from ultrakb.application.document_actions import DocumentIdentity
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.focus import focus_documents, focus_instruction
from ultrakb.application.query_views import resolve_query_views
from ultrakb.config import LlmCredentialBundle

pytest_plugins = ("processing_fixtures",)


@pytest.mark.native
def test_native_focus_and_answer_sources(tmp_path):
    pytest.importorskip("PySide6")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_answer_sources.py",
            "--headless",
            "--output",
            str(tmp_path / "gui"),
        ],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH="."),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "gui/result.json").read_text())
    assert len(report["checks"]) == 5 and report["service"] == "no model calls"


@pytest.fixture
def originals(kb_dir, tmp_path, processing_only):
    from ultrakb.locks import atomic_write_text, kb_ingest_lock

    values = []
    for name, body in (
        ("review", "Reviews require seven days."),
        ("archive", "Archives are retained quarterly."),
    ):
        path = kb_dir / "raw" / f"{name}.md"
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            atomic_write_text(path, f"# {name}\n\n{body}")
        retained = processing_only(path)
        publish_processed_source(kb_dir, retained.source_id)
        values.append(retained)
    return values


def read_originals(kb, selection, names):
    from agents import Agent
    from agents.tool_context import ToolContext

    from ultrakb.agent.query_evidence import restrict_query_agent
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    context = ExecutionContext(
        executor=CompletionExecutor(
            RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle(api_key="fixture-unused")),
            ModelCallPolicy(max_calls=0),
        )
    )
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.source_reading import source_capabilities

    with kb_ingest_lock(kb / ".ultrakb"), context.begin(kb):
        agent = restrict_query_agent(
            Agent(name="actual reader", instructions="", tools=[]), selection
        )
        tool = next(tool for tool in agent.tools if tool.name == "get_text_content")
        import json

        for name in names:
            asyncio.run(
                tool.on_invoke_tool(
                    ToolContext(
                        context=None,
                        tool_name=tool.name,
                        tool_call_id="native-read",
                        tool_arguments="{}",
                    ),
                    json.dumps(
                        source_capabilities(selection.views[0])[name]["readers"]["get_text_content"]
                    ),
                )
            )
    assert context.executor.calls == 0
    return context


def test_focus_preserves_full_selection_and_actual_other_source_reference(kb_dir, originals):
    focus = DocumentIdentity("source", originals[0].source_id)
    values = focus_documents(kb_dir, (focus,))
    assert values[0].available
    assert "not restrict" in focus_instruction(kb_dir, (focus,))
    selection = resolve_query_views(kb_dir)
    assert set(selection.views[0].source_revision_ids) == {
        item.source_revision_id for item in originals
    }
    other_name = originals[1].doc_name
    context = read_originals(kb_dir, selection, (other_name,))
    text = (
        f"Quarterly. [[sources/{other_name}.content.json]]\n\n"
        f"[Again](sources/{other_name}.content.json)"
    )
    snapshot = capture_answer_citations(text, selection, context.returned_reads)
    answer = CompletedAnswer("turn", "question", text, citations=snapshot, kb_dir=str(kb_dir))
    [group] = answer_citations(answer)
    assert group.identity == originals[1].source_id and all(
        item["returned"] for item in group.references
    )
    assert group.identity != focus.value and len(group.references) == 1
    body, note, _ = read_answer_reference(kb_dir, group.references[0])
    assert "quarterly" in body and "本次原文读取记录" in note


def test_code_logs_and_focus_never_create_answer_references(kb_dir, originals):
    name = originals[0].doc_name
    path = f"sources/{name}.content.json"
    text = f"`[[{path}]]`\n\n```text\n[[{path}]]\n```"
    assert citation_targets(text) == ()
    answer = CompletedAnswer(
        "turn",
        "q",
        text,
        trace=({"kind": "tool", "name": "get_text_content", "arguments": {"doc_name": name}},),
    )
    assert answer_citations(answer) == ()


def test_snapshot_survives_live_withdrawal_but_missing_file_does_not_substitute(kb_dir, originals):
    from ultrakb.application.removal import remove_document

    name = originals[0].doc_name
    selection = resolve_query_views(kb_dir)
    context = read_originals(kb_dir, selection, (name,))
    text = f"Seven days. [[sources/{name}.content.json]]"
    reference = capture_answer_citations(text, selection, context.returned_reads)["references"][0]
    remove_document(kb_dir, originals[0].source_id)
    assert "seven days" in read_answer_reference(kb_dir, reference)[0]
    from ultrakb.locks import kb_ingest_lock

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        from ultrakb.storage.blobs import blob_path

        blob_path(kb_dir, reference["digest"]).unlink()
    with pytest.raises(FileNotFoundError):
        read_answer_reference(kb_dir, reference)


def test_legacy_answer_is_explicitly_unknown_and_bad_paths_are_rejected(kb_dir):
    from ultrakb.locks import kb_ingest_lock

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        from ultrakb.desktop.verification_data import publish_pages

        publish_pages(kb_dir, {"summaries/old.md": b"# Current old reference"})
    answer = CompletedAnswer(
        "old", "q", "[[summaries/old]] and [[summaries/old.md]]", kb_dir=str(kb_dir)
    )
    [group] = answer_citations(answer)
    assert group.historical_unknown
    body, note, _ = read_answer_reference(kb_dir, group.references[0], historical_unknown=True)
    assert "Current old reference" in body and "无法确认生成时版本" in note
    forged = replace(
        answer,
        citations={
            "version": 1,
            "references": [
                {
                    "target": "summaries/old",
                    "path": "summaries/old.md",
                    "kind": "reference",
                    "identity": "old",
                    "name": "old",
                    "returned": False,
                    "stored_path": ".ultrakb/config.yaml",
                    "stored_root": ".ultrakb",
                    "digest": "x",
                }
            ],
        },
    )
    with pytest.raises(ValueError, match="retained main-store provenance"):
        read_answer_reference(kb_dir, answer_citations(forged)[0].references[0])


def test_partial_workbook_has_focus_and_unprocessed_source_has_recovery(kb_dir):
    from focus_fixtures import partial_workbook, unprocessed_source

    from ultrakb.application.document_actions import document_inventory

    source = partial_workbook(
        kb_dir, kb_dir / "raw/partial.xlsx", bundle=LlmCredentialBundle(api_key="unused-fixture")
    )
    missing = unprocessed_source(kb_dir, kb_dir / "raw/unprocessed.md")
    rows = {row.identity.value: row for row in document_inventory(kb_dir)}
    assert rows[source].document["status"] == "partial"
    assert rows[source].actions["ask"].allowed
    statuses = {unit["name"]: unit["status"] for unit in rows[source].document["units"]}
    assert statuses == {"Alpha": "completed", "Beta": "failed", "Gamma": "pending"}
    assert not rows[missing].actions["ask"].allowed and rows[missing].actions["retry"].allowed
    assert "更多" in rows[missing].actions["ask"].reason
