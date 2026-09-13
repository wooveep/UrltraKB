"""Identical analysis still publishes independently bound sources."""

import json

import pytest
import yaml

from openkb.application.documents import import_document
from openkb.state import HashRegistry
from tests.http_model_fixture import evidence_response


@pytest.mark.parametrize("setting", [None, "max_tokens", "max_requests", "document_timeout"])
def test_more_execution_budget_recovers_completed_empty_source_units(
    kb_dir, tmp_path, model_service, setting
):
    from openkb.application.execution import ExecutionContext
    from openkb.application.source_actions import continue_source
    from openkb.cancellation import OperationCancelled

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["navigation"] = {"enabled": True}
    config["processing"].update(max_tokens=1000000, max_requests=100, document_timeout=300)
    config_path.write_text(yaml.safe_dump(config))
    source = tmp_path / "scope.md"
    source.write_text("# Scope\n\nConnection timeout is 30 seconds. Do not overlap retries.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            for unit, row in zip(payload["units"], value["units"], strict=True):
                if unit["kind"] == "heading":
                    row.update(facts=[], empty_reason="Section label, no independent claim.")
        return value

    def stop(event):
        if event.get("stage") == "planning":
            raise OperationCancelled()

    model_service.respond = respond
    first = import_document(kb_dir, source, context=ExecutionContext(on_event=stop))
    assert first.knowledge_compilation == "stopped", first
    before = len(model_service)
    if setting is not None:
        config["processing"][setting] *= 2
        config_path.write_text(yaml.safe_dump(config))
    resumed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert resumed.knowledge_compilation == "completed", resumed
    assert all(
        json.loads(call["messages"][-1]["content"])["stage"] != "facts"
        for call in model_service[before:]
    )


@pytest.mark.parametrize("damage", ["stages_list", "null_unknown", "nested_key", "invalid_id"])
def test_continue_rebuilds_damaged_stage_index(kb_dir, tmp_path, model_service, damage):
    from openkb.application.execution import ExecutionContext
    from openkb.application.source_actions import continue_source
    from openkb.cancellation import OperationCancelled

    source = tmp_path / "limits.md"
    source.write_text("Pressure must remain below 37 kPa.")

    def stop(event):
        if event.get("stage") == "generated":
            raise OperationCancelled()

    first = import_document(kb_dir, source, context=ExecutionContext(on_event=stop))
    assert first.knowledge_compilation == "stopped"
    indexes = list((kb_dir / ".openkb/source-store/compilation/latest").glob("*.json"))
    assert len(indexes) == 1
    index = json.loads(indexes[0].read_text())
    key = index["checkpoints"][0]
    index["stages"] = {
        "stages_list": [],
        "null_unknown": {"facts": [key], "unknown": None},
        "nested_key": {"facts": [[key]]},
        "invalid_id": {"facts": ["../foreign"]},
    }[damage]
    indexes[0].write_text(json.dumps(index))
    before = len(model_service)
    result = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert result.knowledge_compilation == "completed", result
    assert all(
        json.loads(call["messages"][-1]["content"])["stage"] not in {"facts", "planning"}
        for call in model_service[before:]
    )


def test_other_source_reuses_fact_analysis_and_rebinds_original_citations(
    kb_dir, tmp_path, model_service
):
    paths = []
    for folder in ("a", "b"):
        path = tmp_path / folder / "handbook.md"
        path.parent.mkdir()
        path.write_text("# Pressure\n\nKeep pressure at 37 kPa.")
        paths.append(path)
    stages = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        stages.append(payload["stage"])
        return evidence_response(payload)

    model_service.respond = respond
    first = import_document(kb_dir, paths[0])
    assert first.knowledge_compilation == "completed", first
    stages.clear()
    second = import_document(kb_dir, paths[1])
    assert second.knowledge_compilation == "completed", second
    assert "facts" not in stages
    assert {"planning", "generation"} <= set(stages)
    assert "verification" not in stages  # The complete verification input is equal.
    assert first.source_id != second.source_id
    registry = HashRegistry(kb_dir / ".openkb/hashes.json").all_entries()
    assert first.source_id in registry and second.source_id in registry
    pages = "\n".join(p.read_text() for p in (kb_dir / "wiki/concepts").glob("*.md"))
    assert first.input_version in pages and second.input_version in pages


def test_display_copy_and_regenerated_navigation_reuse_semantic_fact_input(
    kb_dir, tmp_path, model_service
):
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["navigation"] = {"enabled": True}
    path.write_text(yaml.safe_dump(config))
    first_path = tmp_path / "Operations V2.md"
    second_path = tmp_path / "Operations V2 (copy).md"
    for source in (first_path, second_path):
        source.write_text("# Operations\n\n" + "Timeout is 30 seconds. Never overlap retries. " * 7)
    navigation_generation = 0

    def respond(body):
        nonlocal navigation_generation
        payload = json.loads(body["messages"][-1]["content"])
        result = evidence_response(payload)
        if payload["stage"] == "index_summary":
            navigation_generation += 1
            for row in result["summaries"]:
                row["summary"] = f"Navigation description {navigation_generation}."
        return result

    model_service.respond = respond
    first = import_document(kb_dir, first_path)
    assert first.knowledge_compilation == "completed", first
    before = len(model_service)
    second = import_document(kb_dir, second_path)
    assert second.knowledge_compilation == "completed", second
    assert first.source_id != second.source_id
    assert navigation_generation == 2
    assert all(
        json.loads(call["messages"][-1]["content"])["stage"] != "facts"
        for call in model_service[before:]
    )
    # A filename's unique version remains meaningful, even with the same body.
    third_path = tmp_path / "Operations V3.md"
    third_path.write_bytes(first_path.read_bytes())
    before = len(model_service)
    third = import_document(kb_dir, third_path)
    assert third.knowledge_compilation == "completed", third
    assert any(
        json.loads(call["messages"][-1]["content"])["stage"] == "facts"
        for call in model_service[before:]
    )


def test_equal_occurrences_keep_their_distinct_fact_rows(kb_dir, tmp_path, model_service):
    originals = []
    text = "Voltage is 5 volts. Timeout is 30 seconds."
    for folder in ("first", "second"):
        path = tmp_path / folder / "handbook.md"
        path.parent.mkdir()
        path.write_text("\n\n".join([text] * 4))
        originals.append(path)
    generated = []
    fact_requests = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "facts":
            fact_requests.append(payload)
            for index, row in enumerate(response["units"]):
                quote = "Timeout is 30 seconds." if index == 2 else "Voltage is 5 volts."
                row["facts"] = [{"topic": "Operation", "statement": quote, "quote": quote}]
        if payload["stage"] == "generation":
            generated.append([fact["statement"] for fact in payload["facts"]])
        return response

    model_service.respond = respond
    first = import_document(kb_dir, originals[0])
    assert first.knowledge_compilation == "completed", first
    initial = list(generated)
    generated.clear()
    count = len(fact_requests)
    second = import_document(kb_dir, originals[1])
    assert second.knowledge_compilation == "completed", second
    assert len(fact_requests) == count
    assert generated == initial
    assert "Timeout is 30 seconds." in [value for row in generated for value in row]


def test_distant_batch_subject_changes_invalidate_matching_passage(kb_dir, tmp_path, model_service):
    paths = []
    for subject in ("Alpha", "Beta"):
        path = tmp_path / subject / "handbook.md"
        path.parent.mkdir()
        path.write_text(
            f"Subject: {subject}.\n\n# Operations\n\nGeneral instructions.\n\n"
            "Timeout is 30 seconds.\n\nEnd of instructions."
        )
        paths.append(path)
    seen = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "facts":
            seen.extend(unit["text"] for unit in payload["units"])
        return evidence_response(payload)

    model_service.respond = respond
    assert import_document(kb_dir, paths[0]).knowledge_compilation == "completed"
    seen.clear()
    assert import_document(kb_dir, paths[1]).knowledge_compilation == "completed"
    assert "Timeout is 30 seconds." in seen


def test_position_only_new_version_rebinds_shared_facts_and_retains_old_original(
    kb_dir, tmp_path, model_service
):
    from openkb.evidence import ParseStore
    from openkb.sources import SourceStore

    source = tmp_path / "positions.md"
    source.write_text("# Operations\n\nKeep pressure at 37 kPa.")
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    before = len(model_service)
    source.write_text("\n\n" + source.read_text())
    second = import_document(kb_dir, source)
    assert second.knowledge_compilation == "completed", second
    assert second.source_id == first.source_id and second.input_version != first.input_version
    assert "facts" not in [
        json.loads(call["messages"][-1]["content"])["stage"] for call in model_service[before:]
    ]
    old = ParseStore(kb_dir).load(first.parse_id)
    new = ParseStore(kb_dir).load(second.parse_id)
    assert [block.location["line"] for block in new.blocks] == [
        block.location["line"] + 2 for block in old.blocks
    ]
    store = SourceStore(kb_dir)
    assert not store.original(store.version(first.input_version)).read_text().startswith("\n")
    assert store.original(store.version(second.input_version)).read_text().startswith("\n\n")
    bindings = list((store.root / "analysis/bindings" / second.input_version).glob("*.json"))
    assert bindings
    for path in bindings:
        record = json.loads(path.read_text())
        assert (store.root / "analysis/records" / (record["analysis"] + ".json")).exists()


def test_identical_facts_are_not_shared_between_knowledge_bases(kb_dir, tmp_path, model_service):
    from openkb.application.knowledge_bases import initialize_kb

    other = tmp_path / "isolated-kb"
    initialize_kb(other, seed_environment=False)
    (other / ".openkb/config.yaml").write_bytes((kb_dir / ".openkb/config.yaml").read_bytes())
    (other / ".env").write_bytes((kb_dir / ".env").read_bytes())
    source = tmp_path / "private.md"
    source.write_text("Keep pressure at 37 kPa.")
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    before = len(model_service)
    second = import_document(other, source)
    assert second.knowledge_compilation == "completed", second
    assert "facts" in [
        json.loads(call["messages"][-1]["content"])["stage"] for call in model_service[before:]
    ]


def test_removing_analysis_producer_keeps_other_source_and_reusable_binding(
    kb_dir, tmp_path, model_service
):
    from openkb.application.removal import preview_removal, remove_document
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup
    from openkb.sources import SourceStore

    paths = []
    for name in ("producer", "consumer", "later"):
        source = tmp_path / name / "handbook.md"
        source.parent.mkdir()
        source.write_text("# Pressure\n\nKeep pressure at 37 kPa.")
        paths.append(source)
    first = import_document(kb_dir, paths[0])
    second = import_document(kb_dir, paths[1])
    assert first.knowledge_compilation == second.knowledge_compilation == "completed"
    page = next((kb_dir / "wiki/concepts").glob("*.md"))
    page.write_text(page.read_text() + "\n\nHuman note: preserve this instruction.\n")
    preview = preview_removal(kb_dir, first.source_id)
    removed = remove_document(kb_dir, first.source_id, version=preview.version)
    assert removed.status == "removed", removed
    cleanup = preview_history_cleanup(kb_dir)
    cleanup_history(kb_dir, cleanup.id)
    store = SourceStore(kb_dir)
    assert store.original(store.version(second.input_version)).read_text() == paths[1].read_text()
    pages = "\n".join(page.read_text() for page in (kb_dir / "wiki/concepts").glob("*.md"))
    assert second.input_version in pages and first.input_version not in pages
    assert "Human note: preserve this instruction." in pages
    before = len(model_service)
    third = import_document(kb_dir, paths[2])
    assert third.reason == "needs_acceptance", third
    assert "facts" not in [
        json.loads(call["messages"][-1]["content"])["stage"] for call in model_service[before:]
    ]
    from openkb.application.source_actions import continue_source, review_source_proposal

    review = review_source_proposal(kb_dir, third.resume)
    saved = continue_source(
        kb_dir,
        third.source_id,
        version_id=third.input_version,
        proposal_id=third.resume,
        accept_pages=review["protected"],
    )
    assert saved.knowledge_compilation == "completed", saved
    assert "Human note: preserve this instruction." in page.read_text()


@pytest.mark.parametrize(
    "damage", ["missing_close", "nested_inside", "nested_outside", "cancelled_prefix"]
)
def test_removal_refuses_ambiguous_owned_ranges_without_partial_wiki_changes(
    kb_dir, tmp_path, model_service, damage
):
    from openkb.application.removal import remove_document

    source = tmp_path / "ambiguous.md"
    source.write_text("Pressure must remain below 37 kPa.")
    imported = import_document(kb_dir, source)
    assert imported.knowledge_compilation == "completed", imported
    page = next((kb_dir / "wiki/concepts").glob("*.md"))
    opening = f"<!-- openkb-source:{imported.source_id} -->"
    closing = f"<!-- /openkb-source:{imported.source_id} -->"
    outer_open, outer_close = (
        f"<!-- openkb-source:{'a' * 32} -->",
        f"<!-- /openkb-source:{'a' * 32} -->",
    )
    text = page.read_text()
    if damage == "missing_close":
        text = text.replace(closing, "")
    elif damage in {"nested_inside", "cancelled_prefix"}:
        text = text.replace(opening, outer_open + opening).replace(closing, closing + outer_close)
        if damage == "cancelled_prefix":
            text = f"<!-- /openkb-source:{'c' * 32} -->\n" + text
    else:
        text = text.replace(opening, opening + outer_open + outer_close)
    page.write_text(text)
    before = {p: p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="Ambiguous source contribution"):
        remove_document(kb_dir, imported.source_id)
    assert {p: p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()} == before
    assert HashRegistry(kb_dir / ".openkb/hashes.json").get(imported.source_id)
