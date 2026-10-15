"""Ordinary documents share a KB without product/version admission requirements."""

import json

import pytest


@pytest.fixture
def import_note(kb_dir, monkeypatch, live_llm):
    import pymupdf
    from publication_fixtures import publish_document

    from ultrakb.knowledge_scope import resolve_scope
    from ultrakb.locks import atomic_write_text

    live_llm.install(monkeypatch)
    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nlanguage: en\n"
        "role_generation:\n  compile:\n    max_tokens: 512\n    reasoning_effort: none\n"
        "model_capacity:\n  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n",
    )

    before = len(live_llm.calls)

    def run(name, text, *, scope=None):
        resolve_scope(kb_dir, scope, writable=True)
        path = kb_dir / name
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), text)
            pdf.save(path)
        return publish_document(kb_dir, path, text, bundle=live_llm.credentials)

    yield run
    assert len(live_llm.calls) == before


def test_unrelated_documents_share_the_ordinary_wiki(kb_dir, import_note):
    first = import_note("garden.pdf", "Water the seedlings twice per week.")
    second = import_note("budget.pdf", "The project budget is 420 units.")

    assert first.status == second.status == "added"
    assert first.units[0].view_id == second.units[0].view_id == "legacy"
    assert (kb_dir / "wiki/sources/garden.md").is_file()
    assert (kb_dir / "wiki/sources/budget.md").is_file()


@pytest.mark.parametrize("explicit_scope", [False, True])
@pytest.mark.parametrize("title", ["Atlas V1.2 Installation Manual", "Atlas V1.2 Research Report"])
def test_titles_never_create_product_catalogs_or_gate_import(
    kb_dir, import_note, explicit_scope, title
):
    from ultrakb.application.documents import import_document
    from ultrakb.documents import read_document_source
    from ultrakb.knowledge_scope import live_scope
    from ultrakb.source_catalog import read_source

    scope = live_scope(kb_dir) if explicit_scope else None
    first = import_note("notes.pdf", title + "\nA documented observation.", scope=scope)
    second = import_note("other.pdf", title + "\nAnother independent observation.", scope=scope)
    assert first.status == second.status == "added"
    assert first.units[0].view_id == second.units[0].view_id == "legacy"
    source = read_source(kb_dir, first.source_id)
    assert not {"annotation_id", "family_id"} & source.model_dump().keys()
    assert "version_metadata" not in read_document_source(kb_dir, first.source_id)
    for collection in (
        "products",
        "families",
        "annotations",
        "views",
        "version-reviews",
        "metadata-corrections",
        "family-defaults",
    ):
        assert not (kb_dir / ".ultrakb/catalog" / collection).exists()
    retry = import_document(kb_dir, kb_dir / "notes.pdf", scope=scope)
    assert retry.status == "skipped"
    assert retry.source_revision_id == first.source_revision_id
    assert retry.units[0].successful_revision_id == first.units[0].successful_revision_id


def test_question_wording_does_not_gate_available_documents(kb_dir, import_note):
    from ultrakb.application.query_views import resolve_query_views

    result = import_note(
        "atlas.pdf",
        "Atlas Engine supports weekly exports.",
    )
    selection = resolve_query_views(kb_dir, "Does Atlas V2 support exports?")
    assert result.status == "added"
    assert selection.has_evidence
    assert not selection.missing


@pytest.mark.asyncio
async def test_query_keeps_generated_text_without_a_fact_registration_protocol(
    kb_dir, monkeypatch, live_llm
):
    from ultrakb.agent.query import build_query_agent, build_run_config_from_bundle, run_query
    from ultrakb.locks import atomic_write_text

    live_llm.install(monkeypatch)
    atomic_write_text(kb_dir / "wiki/sources/note.md", "The supported command is export --all.")
    atomic_write_text(kb_dir / "wiki/index.md", "# Index\n\nCommand: [[sources/note]].")
    agent = build_query_agent(
        str(kb_dir / "wiki"), "deepseek/deepseek-flash", bundle=live_llm.credentials
    )
    assert "record_source_fact" not in {tool.name for tool in agent.tools}
    actual = await run_query(
        "How do I export everything? Quote the documented command in a fenced shell block "
        "and cite note.md.",
        kb_dir,
        "deepseek/deepseek-flash",
        bundle=live_llm.credentials,
        run_config=build_run_config_from_bundle("deepseek/deepseek-flash", live_llm.credentials),
    )
    assert "export --all" in actual
    assert "```" in actual
    assert "note" in actual.lower()


def test_long_document_plan_does_not_require_added_evidence_fields(kb_dir):
    from test_compile_windows import _plan

    from ultrakb.agent.compile_delivery import publish_compile_results
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.compilation_report import collect_compile_report
    from ultrakb.locks import atomic_write_text

    wiki = kb_dir / "wiki"
    atomic_write_text(
        wiki / "sources/garden.json",
        json.dumps(
            [{"page": 1, "unit_kind": "page", "content": "Seedlings require water twice per week."}]
        ),
    )
    plan = _plan(wiki, {"concepts": {"create": [{"name": "seedlings", "title": "Seedlings"}]}})
    assert "evidence_units" not in plan.concepts.create[0]
    reader = CompileSourceWindows(
        wiki,
        "garden",
        navigation=[{"title": "Seedlings", "node_id": "care", "start_index": 1, "end_index": 1}],
    )
    assert reader.validate_updates(plan, long=True) is None
    original = reader.prepare(plan.concepts.create[0], long=True)
    assert "twice per week" in str(original.message)
    with collect_compile_report() as report:
        publish_compile_results(
            wiki,
            "garden",
            plan,
            summary="Seedling care",
            concept_results=[("seedlings", "# Seedlings\n\nWater twice per week.", False, "Care")],
            doc_type="pageindex",
            original_path=reader.original_path,
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
    from ultrakb.application.query_views import read_query_page, resolve_query_views

    first = import_note("garden-v1.pdf", "Seedlings need two litres of water.")
    second = import_note("garden-v2.pdf", "Seedlings need three litres of water.")
    selected = resolve_query_views(kb_dir, question)
    assert len(selected.views) == 1 and not selected.missing
    view = selected.views[0]
    assert set(view.source_revision_ids) == {first.source_revision_id, second.source_revision_id}
    for name, amount in (("garden-v1", "two"), ("garden-v2", "three")):
        assert amount in read_query_page(selected, f"sources/{name}.md", view_id=view.view_id)
    assert not list((kb_dir / ".ultrakb/catalog/views").glob("*.json"))
    assert not list((kb_dir / ".ultrakb/catalog/version-reviews").glob("*.json"))


def test_product_version_management_is_not_a_public_workflow():
    from ultrakb.api import create_app
    from ultrakb.cli import cli

    assert "versions" not in cli.commands and "views" not in cli.commands
    assert "view_id" not in {parameter.name for parameter in cli.params}
    paths = [route.path for route in create_app().routes if hasattr(route, "path")]
    assert not any(path.startswith(("/api/v1/version", "/api/v1/views")) for path in paths)
    options = {parameter.name for parameter in cli.commands["add"].params}
    assert options.isdisjoint({"product", "versions", "family", "document_revision"})
