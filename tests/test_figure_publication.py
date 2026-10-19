"""Figure delivery binds validated ranges to real frozen assets and publication."""

import json
import re

import pytest
from PIL import Image


def figure_context(kb_dir, *, maintenance_color="blue"):
    from figure_fixtures import record_markdown

    from ultrakb.agent.page_figures import PageFigures, source_ranges
    from ultrakb.figure_assets import freeze_figures

    wiki = kb_dir / "wiki"
    Image.new("RGB", (30, 30), "red").save(wiki / "sources/installation.png")
    Image.new("RGB", (30, 30), maintenance_color).save(wiki / "sources/maintenance.png")
    original = (
        "# Installation\nDisconnect power.\n![Original](installation.png)\n\n"
        "# Maintenance\nLog each inspection.\n![Original](maintenance.png)\n"
    )
    frozen = record_markdown(wiki, "sources/manual.md", original)
    identity = {"source_revision_id": ("b" if maintenance_color == "red" else "c") * 32}
    figures = freeze_figures(wiki, "sources/manual.content.json", frozen, identity)
    ranges = source_ranges("sources/manual.content.json", frozen, figures, identity)
    return original, PageFigures(wiki, ranges, tuple(figures))


def test_short_pages_use_final_source_ranges_and_keep_all_original_figures(kb_dir):
    from ultrakb.agent.compiler import _write_concept, _write_summary
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.markdown_images import image_references

    original, context = figure_context(kb_dir)
    selected = next(r for r in context.ranges if "Maintenance" in r["label"])
    delivered = json.loads(
        validate_page_delivery(
            json.dumps(
                {
                    "description": "Maintenance",
                    "content": "# Maintenance\nLog each inspection.",
                    "source_ranges": [selected["id"]],
                }
            ),
            figure_context=context,
        )
    )
    decorated = context.decorate("concepts/maintenance.md", delivered)
    _write_summary(
        context.wiki,
        "manual",
        context.decorate(
            "summaries/manual.md",
            {
                "content": "Installation and maintenance instructions.",
                "source_ranges": list(context.ids),
            },
        )["content"],
    )
    _write_concept(
        context.wiki,
        "maintenance",
        decorated["content"],
        "summaries/manual.md",
        False,
        decorated["description"],
    )
    page = context.wiki / "concepts/maintenance.md"
    images = image_references(page.read_text())
    assert len(images) == 1
    target = (page.parent / images[0].local_path).resolve()
    assert target.read_bytes() == (context.wiki / "sources/maintenance.png").read_bytes()
    assert target.parent.name == "figure-assets"
    assert len(image_references((context.wiki / "summaries/manual.md").read_text())) == 2
    assert "source_revision_id" in page.read_text() and "未进行图片理解" in page.read_text()
    assert (context.wiki / "sources/manual.md").read_text() == original


def test_replacing_source_keeps_old_page_figures_in_the_same_snapshot(kb_dir):
    from ultrakb.agent.compiler import _write_concept, _write_summary
    from ultrakb.markdown_images import image_references

    _, old = figure_context(kb_dir, maintenance_color="red")
    selected = next(r for r in old.ranges if "Maintenance" in r["label"])
    old_body = old.decorate(
        "concepts/maintenance.md",
        {
            "content": "Log each inspection.",
            "source_ranges": [selected["id"]],
        },
    )["content"]
    _write_concept(old.wiki, "maintenance", old_body, "summaries/manual.md", False)
    page = old.wiki / "concepts/maintenance.md"
    retained = page.read_bytes()
    old_target = page.parent / image_references(page.read_text())[0].local_path
    old_bytes = old_target.read_bytes()
    _, current = figure_context(kb_dir, maintenance_color="blue")
    summary = current.wiki / "summaries/manual.md"
    _write_summary(
        current.wiki,
        "manual",
        current.decorate(
            "summaries/manual.md",
            {
                "content": "Inspect the new device.",
                "source_ranges": list(current.ids),
            },
        )["content"],
    )
    assert page.read_bytes() == retained and old_target.read_bytes() == old_bytes
    new_target = next(
        summary.parent / reference.local_path
        for reference in image_references(summary.read_text())
        if (summary.parent / reference.local_path).read_bytes()
        == (current.wiki / "sources/maintenance.png").read_bytes()
    )
    assert new_target.read_bytes() == (current.wiki / "sources/maintenance.png").read_bytes()
    assert new_target.resolve() != old_target.resolve()


def test_invalid_final_range_blocks_the_entire_candidate(kb_dir):
    from ultrakb.agent.compile_delivery import publish_compile_results
    from ultrakb.agent.compile_plan import normalize_plan
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compilation_report import CompilationIncomplete, collect_compile_report
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.mutation import mutation_scope

    _, context = figure_context(kb_dir)
    valid = {
        "description": "Good",
        "content": "Disconnect power.",
        "source_ranges": [context.ids[0]],
    }
    good = json.loads(validate_page_delivery(json.dumps(valid), figure_context=context))
    invalid = {**valid, "source_ranges": ["wrong-source-revision"]}
    with pytest.raises(PageResponseError) as failed:
        validate_page_delivery(json.dumps(invalid), figure_context=context)
    plan = normalize_plan(
        {"concepts": {"create": [{"name": "good"}, {"name": "bad"}]}},
        existing={"concepts": set(), "entities": set()},
        entity_types=frozenset(),
        sanitize=lambda name: name,
    )
    with pytest.raises(CompilationIncomplete):
        with (
            kb_ingest_lock(kb_dir / ".ultrakb"),
            mutation_scope(kb_dir, [context.wiki], operation="figure-candidate"),
            collect_compile_report() as report,
        ):
            publish_compile_results(
                context.wiki,
                "invalid",
                plan,
                summary="A necessary requirement.",
                concept_results=[
                    ("good", context.decorate("concepts/good.md", good)["content"], False, "Good"),
                    failed.value,
                ],
                rewrite_summary=True,
            )
            report.require_complete()
    assert not (context.wiki / "concepts/good.md").exists()
    assert not (context.wiki / "concepts/bad.md").exists()
    assert not (context.wiki / "summaries/invalid.md").exists()


@pytest.mark.parametrize("kind", ["concept", "entity"])
def test_creation_and_repair_contracts_allow_required_figure_ranges(kb_dir, kind):
    from ultrakb.agent.compiler import (
        _CONCEPT_PAGE_USER,
        _ENTITY_PAGE_USER,
        _write_concept,
        _write_entity,
    )
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError, page_repair_message

    _, context = figure_context(kb_dir)
    template = _CONCEPT_PAGE_USER if kind == "concept" else _ENTITY_PAGE_USER
    prompt = template.format(
        title="Reactor", type="product", doc_name="procedure", update_instruction=""
    )
    assert not re.search(r"object with (two|three|[0-9]+) keys", prompt)
    declaration = json.loads(context.message()["content"].split("\n", 1)[1])
    assert declaration["source_ranges_schema"]["minItems"] == 1
    candidate = {
        "description": "Reactor",
        "content": "Disconnect before maintenance.",
        **({"type": "product"} if kind == "entity" else {}),
    }
    with pytest.raises(PageResponseError, match="page_invalid_source_ranges"):
        validate_page_delivery(json.dumps(candidate), figure_context=context)
    correction = page_repair_message(
        "page_invalid_source_ranges", "The required source_ranges is missing"
    )["content"]
    assert (
        "source_ranges" in correction
        and "other required fields" in correction
        and "type" in correction
    )
    received = json.loads(
        validate_page_delivery(
            json.dumps({**candidate, "source_ranges": [context.ids[0]]}), figure_context=context
        )
    )
    collection = "concepts" if kind == "concept" else "entities"
    content = context.decorate(f"{collection}/reactor.md", received)["content"]
    if kind == "concept":
        _write_concept(context.wiki, "reactor", content, "summaries/procedure.md", False)
    else:
        _write_entity(
            context.wiki, "reactor", content, "summaries/procedure.md", False, type_="product"
        )
    assert (context.wiki / collection / "reactor.md").exists()


def test_invalid_embedded_image_keeps_original_and_text_without_sending_payload(kb_dir):
    from publication_fixtures import publish_document

    from ultrakb.agent.compiler import short_document_messages
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.application.views import published_scope
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.locks import atomic_write_text
    from ultrakb.storage.catalog_store import CatalogStore

    source = kb_dir / "broken-image.md"
    original = "# Procedure\nDisconnect the load.\n![Figure](data:image/png;base64,NOT_VALID!!!)\n"
    atomic_write_text(source, original)
    result = publish_document(kb_dir, source, "Disconnect the load.", bundle=LlmCredentialBundle())
    assert result.status == "added"
    catalog = CatalogStore(kb_dir, published_scope(kb_dir).knowledge.identity)
    assert catalog.read_original(result.source_revision_id).decode() == original
    doc_name = result.units[0].doc_name
    frozen = json.loads((kb_dir / f"wiki/sources/{doc_name}.content.json").read_text())
    assert frozen["diagnostics"]
    reader = CompileSourceWindows(kb_dir / "wiki", doc_name)
    evidence = reader.current_original_evidence(frozen["text"])
    messages = short_document_messages(
        "broken-image", frozen["text"], "", "en", original_evidence=evidence
    )
    assert "NOT_VALID" not in str(messages)
    assert "Disconnect the load." in str(messages)
