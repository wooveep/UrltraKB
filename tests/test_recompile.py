"""Recompile CLI behavior uses retained native inputs and actual SDK transport."""

import json

import pytest
from click.testing import CliRunner
from test_storage_conversation_writes import StreamTransport
from test_storage_indexing import indexing_response

from ultrakb.agent import compiler
from ultrakb.application.views import published_scope
from ultrakb.cli import cli
from ultrakb.desktop.verification_data import publish_pages, retain_document


def seed(kb, *, segmented=False):
    retain_document(
        kb,
        "manual.pdf" if segmented else "manual.md",
        "manual",
        "The value is 17.",
        pages=[{"page": 1, "content": "The value is 17."}] if segmented else None,
        summary="---\ndescription: Fixture\n---\n# Manual\n\nThe value is 17.",
    )


def response(request, number):
    if number % 2:
        return {"content": '{"concepts":[],"entities":[]}'}
    if any(
        str(m.get("content", "")).startswith("Original document: ") for m in request["messages"]
    ):
        return indexing_response(request, number)
    return {"content": '{"description":"Fixture","content":"The value is 17."}'}


def invoke(kb, args, *, input_text=None):
    return CliRunner().invoke(cli, ["--kb-dir", str(kb), *args], input=input_text)


@pytest.mark.parametrize("segmented", [False, True])
def test_recompile_reads_retained_native_input_and_reuses_ready_index(kb_dir, segmented):
    seed(kb_dir, segmented=segmented)
    before = published_scope(kb_dir)
    index_ids = before.knowledge.index_revisions()
    for path in (kb_dir / "wiki/sources").rglob("*"):
        if path.is_file():
            path.unlink()
    with StreamTransport(response) as service:
        (kb_dir / ".env").write_text(f"LLM_API_KEY=offline-key\nOPENAI_API_BASE={service.url}\n")
        result = invoke(kb_dir, ["recompile", "manual"])
        assert result.exit_code == 0 and "recompiled 1" in result.output, result.output
        assert len(service.requests) == 2
    assert "17" in published_scope(kb_dir).knowledge.read("summaries/manual.md").decode()
    assert published_scope(kb_dir).knowledge.index_revisions() == index_ids


def test_recompile_all_requires_confirmation(kb_dir):
    seed(kb_dir)
    with StreamTransport([]) as service:
        result = invoke(kb_dir, ["recompile", "--all"], input_text="n\n")
    assert result.exit_code == 0 and "Aborted" in result.output
    assert not service.requests


def test_recompile_all_yes_bypasses_confirmation(kb_dir):
    seed(kb_dir)
    with StreamTransport(response) as service:
        (kb_dir / ".env").write_text(f"LLM_API_KEY=offline-key\nOPENAI_API_BASE={service.url}\n")
        result = invoke(kb_dir, ["recompile", "--all", "--yes"])
        assert result.exit_code == 0 and "recompiled 1" in result.output, result.output
        assert len(service.requests) == 2


def test_recompile_dry_run_does_not_write_or_call_a_model(kb_dir):
    seed(kb_dir, segmented=True)
    before = published_scope(kb_dir)
    with StreamTransport([]) as service:
        result = invoke(kb_dir, ["recompile", "--all", "--dry-run"])
    assert result.exit_code == 0 and "(long / segmented)" in result.output, result.output
    assert not service.requests
    assert published_scope(kb_dir).publication_id == before.publication_id
    assert published_scope(kb_dir).knowledge.files() == before.knowledge.files()


@pytest.mark.parametrize(
    "args,message",
    [
        (["recompile"], "Specify a document name or pass --all"),
        (["recompile", "manual", "--all"], "not both"),
        (["recompile", "unknown"], "No document matching"),
    ],
)
def test_recompile_invalid_selection_does_not_send(kb_dir, args, message):
    seed(kb_dir)
    with StreamTransport([]) as service:
        result = invoke(kb_dir, args)
    assert message in result.output and not service.requests


@pytest.mark.parametrize("refresh", [False, True])
def test_recompile_refreshes_schema_only_when_explicitly_requested(kb_dir, refresh):
    from ultrakb.schema import AGENTS_MD

    seed(kb_dir)
    publish_pages(kb_dir, {"AGENTS.md": b"OLD CUSTOM SCHEMA\n"})
    previous = published_scope(kb_dir)
    with StreamTransport(response) as service:
        (kb_dir / ".env").write_text(f"LLM_API_KEY=offline-key\nOPENAI_API_BASE={service.url}\n")
        result = invoke(kb_dir, ["recompile", "manual", *(["--refresh-schema"] if refresh else [])])
        assert result.exit_code == 0 and "recompiled 1" in result.output, result.output
        assert len(service.requests) == 2
    assert published_scope(kb_dir).knowledge.read("AGENTS.md") == (
        AGENTS_MD.encode() if refresh else b"OLD CUSTOM SCHEMA\n"
    )
    assert previous.knowledge.read("AGENTS.md") == b"OLD CUSTOM SCHEMA\n"


def test_compile_long_doc_backfills_summary_frontmatter(tmp_path):
    from ultrakb import frontmatter
    from ultrakb.locks import atomic_write_text

    wiki = tmp_path / "wiki"
    (wiki / "sources").mkdir(parents=True)
    atomic_write_text(
        wiki / "sources/long.json",
        json.dumps([{"page": 1, "unit_kind": "page", "content": "A retained long report."}]),
    )
    summary_path = wiki / "summaries/long.md"
    summary_path.parent.mkdir()
    atomic_write_text(
        summary_path,
        "---\ndoc_type: pageindex\nfull_text: sources/long.json\n---\n\n# Long\n",
    )
    compiler._write_summary(
        wiki,
        "long",
        "# Long\n\nA retained long report.",
        doc_type="pageindex",
        description="A long report.",
        full_text="sources/long.json",
    )
    text = summary_path.read_text(encoding="utf-8")
    assert 'type: "Summary"' in text
    assert 'description: "A long report."' in text
    assert frontmatter.parse(text)["full_text"] == "sources/long.json"
