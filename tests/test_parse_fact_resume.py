"""OCR completion changes a parse identity without invalidating unrelated facts."""

import json

import yaml

from openkb.application.documents import import_document
from openkb.application.execution import ExecutionContext
from openkb.application.source_actions import continue_source
from openkb.cancellation import OperationCancelled
from openkb.evidence import BlockDraft, ParseStore
from openkb.sources import SourceStore


def test_continue_reuses_unaffected_facts_after_a_parse_changes(kb_dir, tmp_path, model_service):
    config_path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config_path.read_text())
    settings["navigation"] = {"enabled": False}
    config_path.write_text(yaml.safe_dump(settings))
    path = tmp_path / "manual.md"
    path.write_text("\n\n".join(f"Unit {i} requires version {i + 1}." for i in range(6)))

    def stop(event):
        if event.get("stage") == "planning":
            raise OperationCancelled()

    first = import_document(kb_dir, path, context=ExecutionContext(on_event=stop))
    assert first.knowledge_compilation == "stopped", first
    store = SourceStore(kb_dir)
    source = store.version(first.input_version)
    parser = ParseStore(kb_dir)
    old = parser.load(first.parse_id)
    drafts = [
        BlockDraft(
            store.asset(block.blob).read_text(encoding="utf-8"),
            block.kind,
            block.location,
            block.assets,
            block.context,
            block.context_data,
        )
        for block in old.blocks
    ]
    assert len(drafts) == 6
    last = drafts[-1]
    drafts[-1] = BlockDraft("Unit 5 requires corrected version 12.", last.kind, last.location)
    new = parser.save(source, old.profile, drafts, quality=old.quality)
    parser.select(source, new)
    assert new.id != old.id and new.blocks[0].id == old.blocks[0].id
    before = len(model_service)
    resumed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert resumed.knowledge_compilation == "completed", resumed
    requests = [json.loads(call["messages"][-1]["content"]) for call in model_service[before:]]
    extracted = [
        unit["text"]
        for request in requests
        if request.get("stage") == "facts"
        for unit in request["units"]
    ]
    assert "Unit 0 requires version 1." not in extracted, extracted
    assert "Unit 3 requires version 4." not in extracted, extracted
    assert "Unit 4 requires version 5." in extracted  # Its immediate neighbor changed.
    assert "Unit 5 requires corrected version 12." in extracted
    assert resumed.parse_id == new.id


def test_continue_keeps_selected_parse_even_if_ocr_settings_change(
    kb_dir, tmp_path, monkeypatch, model_service
):
    from openkb.application import document_pipeline

    path = tmp_path / "manual.md"
    path.write_text("The service requires port 443.")
    first = import_document(kb_dir, path)
    config_path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config_path.read_text())
    settings["parsing"] = {"ocr": {"policy": "off"}}
    config_path.write_text(yaml.safe_dump(settings))

    def unexpected(*args, **kwargs):
        raise AssertionError("Continue must use the selected parse without invoking OCR/parsing")

    monkeypatch.setattr(document_pipeline, "parse_document", unexpected)
    continued = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert continued.parse_id == first.parse_id
