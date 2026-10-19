"""The actual compiler transport retains native sparse and partial cell observations."""

import json
from contextlib import contextmanager

import pytest
from test_compile_windows import _page_request

pytest_plugins = ("processing_fixtures",)


@contextmanager
def retained_reader(kb_dir, retained):
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.application.views import published_scope
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.compile_inputs import materialize_inputs
    from ultrakb.storage.compile_workspace import materialize_baseline

    base = published_scope(kb_dir).knowledge
    unit = CatalogStore(kb_dir, base.identity).unit_revision(retained.unit_revision_id)
    with materialize_baseline(base, ()) as baseline:
        materialize_inputs(base, baseline, unit)
        yield CompileSourceWindows(baseline.directory, unit.doc_name)


def original_payload(messages, prefix):
    for message in messages:
        content = message["content"]
        blocks = content if isinstance(content, list) else [{"text": content}]
        for block in blocks:
            text = block.get("text", "")
            if prefix in text:
                payload = (
                    text.split("\n", 1)[1]
                    if text.startswith(prefix)
                    else text.split(prefix + "\n", 1)[1]
                )
                return json.JSONDecoder().raw_decode(payload)[0]
    raise AssertionError("Original payload was not sent")


def test_csv_page_input_retains_ragged_records_and_physical_lines(kb_dir, processing_only):
    source = kb_dir / "equipment.csv"
    source.write_text('"Rotor\nAlpha",7,,41\nRotorBeta,9\n')
    with retained_reader(kb_dir, processing_only(source)) as reader:
        page = _page_request(reader, {"name": "rotor", "type": "product"}, kind="entities")
        original = original_payload(page, "Current original:")
        rows = original["csv_rows"]
        assert [(row["row"], len(row["cells"])) for row in rows] == [(1, 4), (2, 2)]
        assert [cell["column"] for cell in rows[0]["cells"]] == [1, 2, 3, 4]
        assert rows[0]["cells"][0]["observations"][0]["value"] == "Rotor\nAlpha"
        assert rows[0]["cells"][0]["observations"][0]["physical_lines"] == [1, 2]
        assert rows[0]["cells"][2]["observations"][0]["value"] == ""
        assert "RotorBeta" not in str(rows[0])


def test_partial_csv_page_input_does_not_refill_an_unread_value(kb_dir, processing_only):
    source = kb_dir / "equipment.csv"
    source.write_text("RotorAlpha,17\nRotorBeta,23\n")
    with retained_reader(kb_dir, processing_only(source)) as reader:
        raw = json.loads((reader.wiki / reader.original_path).read_text())
        first = next(
            origin for origin in raw["origins"] if origin.get("csv", {}).get("column") == 1
        )
        start = first["normalized_span"][0]
        page = _page_request(
            reader,
            {
                "name": "rotor",
                "type": "product",
                "reading_locations": {"chars": f"{start}:{start + 4}"},
            },
            kind="entities",
            long=True,
        )
        windows = original_payload(page, "Original source windows (bounded, frozen):")
        observation = windows[0]["csv_rows"][0]["cells"][0]["observations"][0]
        assert observation["value_complete"] is False
        assert "value" not in observation
        assert "RotorAlpha" not in str(page)
        assert "RotorBeta" not in str(page)


@pytest.mark.parametrize("native", ["pages", "text"])
def test_short_page_reads_each_current_original_only_once(kb_dir, native):
    import hashlib

    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.locks import atomic_write_text
    from ultrakb.text_measurement import measure_markdown
    from ultrakb.text_source import FrozenText, TextOrigin

    wiki = kb_dir / "wiki"
    text = "Model Alder needs supervision during winter."
    source = wiki / "sources/current.md"
    atomic_write_text(source, text)
    rich = wiki / ("sources/current.json" if native == "pages" else "sources/current.content.json")
    atomic_write_text(
        rich,
        json.dumps([{"page": 1, "unit_kind": "page", "content": text}])
        if native == "pages"
        else FrozenText(
            text=text,
            original_characters=len(text),
            original_digest=hashlib.sha256(source.read_bytes()).hexdigest(),
            origins=(TextOrigin(normalized_span=(0, len(text)), original_span=(0, len(text))),),
            assets={},
            tokens=measure_markdown(text),
        ).model_dump_json(),
    )
    atomic_write_text(wiki / "sources/prior.md", "Model Birch requires a summer inspection.")
    atomic_write_text(
        wiki / "concepts/inspection.md",
        "---\ntype: Concept\ndescription: Inspection\nsources: [sources/current.md, "
        + str(rich.relative_to(wiki))
        + ", sources/prior.md]\n---\n# Inspection\n\nExisting inspection conditions.",
    )
    page = _page_request(
        CompileSourceWindows(wiki, "current"), {"name": "inspection"}, action="update"
    )
    original = original_payload(page, "Current original:")
    assert original["path"] == str(rich.relative_to(wiki))
    windows = original_payload(page, "Original source windows (bounded, frozen):")
    assert [w["path"] for w in windows] == ["sources/prior.md"]
    assert windows[0]["source_role"] == "prior_linked_source"
    assert "Model Birch" in windows[0]["content"]


@pytest.mark.parametrize("stage", ["summary", "concept", "entity"])
def test_unchanged_cannot_replace_new_knowledge_pages(kb_dir, stage):
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.mutation import mutation_scope

    protocol = "summary-page-json" if stage == "summary" else "page-json"
    error = "summary_invalid_shape" if stage == "summary" else "page_unchanged_not_allowed"
    with kb_ingest_lock(kb_dir / ".ultrakb"), pytest.raises(PageResponseError, match=error):
        with mutation_scope(kb_dir, [kb_dir / "wiki"], operation="validate-new-page-delivery"):
            validate_page_delivery(
                json.dumps(
                    {"status": "unchanged", "content": "Navigation only", "description": "Reactor"}
                ),
                response_protocol=protocol,
            )
    assert not (kb_dir / "wiki/summaries/instructions.md").exists()
    assert not (kb_dir / "wiki/concepts/reactor.md").exists()
    assert not (kb_dir / "wiki/entities/reactor.md").exists()
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))
