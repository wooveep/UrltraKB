"""Ordinary documents share a KB without product/version admission requirements."""

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def import_note(kb_dir, monkeypatch):
    import pymupdf

    from openkb.application.documents import import_document

    count = 0

    def completion(**kwargs):
        nonlocal count
        count += 1
        reply = {"description": "Note", "content": "A retained source note."} if count % 2 else {}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("litellm.completion", completion)

    def run(name, text, **kwargs):
        path = kb_dir / name
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), text)
            pdf.save(path)
        return import_document(kb_dir, path, **kwargs)

    return run


def test_unrelated_documents_share_the_ordinary_wiki(kb_dir, import_note):
    first = import_note("garden.pdf", "Water the seedlings twice per week.")
    second = import_note("budget.pdf", "The project budget is 420 units.")

    assert first.status == second.status == "added"
    assert first.units[0].view_id == second.units[0].view_id == "legacy"
    assert (kb_dir / "wiki/sources/garden.md").is_file()
    assert (kb_dir / "wiki/sources/budget.md").is_file()


def test_ordinary_import_does_not_infer_a_product_from_its_title(import_note, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Ordinary import must not classify product/manual versions")

    monkeypatch.setattr("openkb.version_evidence.source_title_candidates", unexpected)
    result = import_note("field-notes-v2.pdf", "Field notes\nEdition 2\nPlanting observations.")
    assert result.status == "added"


def test_question_wording_does_not_gate_available_documents(kb_dir, import_note):
    from openkb.application.query_views import resolve_query_views

    result = import_note(
        "atlas.pdf",
        "Atlas Engine supports weekly exports.",
    )
    selection = resolve_query_views(kb_dir, "Does Atlas V2 support exports?")
    assert result.status == "added"
    assert selection.has_evidence
    assert not selection.missing


@pytest.mark.asyncio
async def test_query_keeps_generated_text_without_a_fact_registration_protocol(kb_dir, monkeypatch):
    from openkb.agent.query import run_query
    from openkb.locks import atomic_write_text

    atomic_write_text(kb_dir / "wiki/sources/note.md", "The supported command is export --all.")
    answer = "Run:\n\n  ```sh\n  export --all\n  ```\n\nSource: [note](sources/note.md)"
    calls = []

    async def generate(agent, question, **kwargs):
        calls.append(agent)
        return SimpleNamespace(final_output=answer)

    monkeypatch.setattr("openkb.agent.query.Runner.run", generate)
    actual = await run_query("How do I export everything?", kb_dir, "test-model")
    assert actual == answer
    assert len(calls) == 1
    assert "record_source_fact" not in {tool.name for tool in calls[0].tools}


@pytest.mark.asyncio
async def test_long_document_plan_does_not_require_added_evidence_fields(kb_dir, monkeypatch):
    from openkb.agent.compiler import _compile_concepts
    from openkb.compilation_report import collect_compile_report

    wiki = kb_dir / "wiki"
    (wiki / "sources/garden.json").write_text(
        json.dumps([{"page": 1, "content": "Seedlings require water twice per week."}])
    )
    plan = {"concepts": {"create": [{"name": "seedlings", "title": "Seedlings"}]}}
    monkeypatch.setattr("openkb.agent.compiler._llm_call", lambda *a, **kw: json.dumps(plan))

    async def generate(*args, **kwargs):
        return json.dumps({"description": "Care", "content": "Water twice per week."})

    monkeypatch.setattr("openkb.agent.compiler._llm_call_async", generate)
    with collect_compile_report() as report:
        await _compile_concepts(
            wiki, kb_dir, "test", {}, {}, "Seedling care", "garden", 1, doc_type="pageindex"
        )
        report.require_complete()
    assert "Water twice per week" in (wiki / "concepts/seedlings.md").read_text()


@pytest.mark.parametrize(
    "question",
    [
        "Compare field notes v1 and v2.",
        "What is the latest garden budget?",
        "Atlas V99 的使用条件是什么？",
        "Which documents mention a version?",
    ],
)
def test_question_never_filters_the_shared_sources(kb_dir, import_note, question):
    from openkb.application.query_views import read_query_page, resolve_query_views

    first = import_note("garden-v1.pdf", "Seedlings need two litres of water.")
    second = import_note("garden-v2.pdf", "Seedlings need three litres of water.")
    selected = resolve_query_views(kb_dir, question)
    assert len(selected.views) == 1 and not selected.missing and not selected.candidates
    view = selected.views[0]
    assert set(view.source_revision_ids) == {first.source_revision_id, second.source_revision_id}
    for name, amount in (("garden-v1", "two"), ("garden-v2", "three")):
        assert amount in read_query_page(selected, f"sources/{name}.md", view_id=view.view_id)
    assert not list((kb_dir / ".openkb/catalog/views").glob("*.json"))
    assert not list((kb_dir / ".openkb/catalog/version-reviews").glob("*.json"))


def test_product_version_management_is_not_a_public_workflow():
    from openkb.api import create_app
    from openkb.cli import cli

    assert "versions" not in cli.commands and "views" not in cli.commands
    paths = [route.path for route in create_app().routes if hasattr(route, "path")]
    assert not any(path.startswith(("/api/v1/version", "/api/v1/views")) for path in paths)
    options = {parameter.name for parameter in cli.commands["add"].params}
    assert options.isdisjoint({"product", "versions", "family", "document_revision"})
