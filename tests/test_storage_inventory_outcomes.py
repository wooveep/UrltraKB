"""Committed per-unit quality survives publication and unrelated head changes."""

import json

from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.sources import source_inventory
from ultrakb.desktop.verification_data import publish_pages


def test_management_keeps_the_successful_units_known_compile_outcome(tmp_path):
    root, _, _ = question_knowledge(tmp_path)
    source = tmp_path / "new.md"
    source.write_text("The value is 22.")
    with StreamTransport(
        [
            {"content": json.dumps({"description": "Value", "content": "The value is 22."})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        imported = import_document(root, source)
    assert imported.status == "added", imported
    assert len(service.requests) == 2
    for advance in (False, True):
        if advance:
            publish_pages(root, {"concepts/unrelated.md": b"An unrelated accepted note."})
        [row] = [item for item in source_inventory(root) if item["source_id"] == imported.source_id]
        [unit] = row["units"]
        assert unit["status"] == "completed" and unit["quality_known"] is True
        assert unit["quality"] == imported.units[0].quality
        assert unit["unfinished"] == ()
