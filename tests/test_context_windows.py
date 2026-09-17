"""Public imports identify clipped context without changing original source bytes."""

import json

import pytest

from openkb.application.documents import import_document
from openkb.application.settings import apply_kb_config_patch
from openkb.application.settings_data import KbConfigPatchRequest


def _objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _objects(item)


@pytest.mark.parametrize("navigation", [False, True])
def test_import_marks_partial_context_at_every_model_stage(
    kb_dir, tmp_path, model_service, navigation
):
    # A bounded neighbor ends one character before the literal ends. Without
    # extent metadata it appears to corroborate a different, shorter value.
    shortened = "0x 000003d7 00000001 00000011"
    original = "Example " + "x" * (128 - len(shortened) - 8) + shortened + "0; keep this value."
    assert len(original[:128]) == 128 and original[:128].endswith(shortened)
    source = tmp_path / "window-boundary.md"
    source.write_text("Before the example.\n\n" + original + "\n\nAfter the example.")
    original_bytes = source.read_bytes()
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"navigation": {"enabled": navigation}}),
    )
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    seen = set()
    for request in model_service:
        payload = json.loads(request["messages"][-1]["content"])
        stage = payload.get("stage")
        if stage not in {"facts", "generation", "verification"}:
            continue
        for item in _objects(payload):
            if item.get("text") == original[:128]:
                assert item.get("source_window") == {"start": 0, "end": 128, "total": len(original)}
                assert "source_window" in request["messages"][0]["content"]
                seen.add(stage)
    assert seen == {"facts", "generation", "verification"}
    assert source.read_bytes() == original_bytes


def test_title_context_keeps_partial_original_extent(kb_dir, tmp_path):
    from dataclasses import asdict, replace

    from openkb.agent.evidence_title_context import topic_title_context
    from openkb.evidence_snapshot import EvidenceSnapshot
    from tests.test_evidence_snapshot import prepared

    reader, original = prepared(kb_dir, tmp_path)
    reference = asdict(replace(original, start=1, end=9))
    context = topic_title_context([{"id": "fact", "scope": reference}], EvidenceSnapshot(reader))
    passage = context["passages"][0]
    assert passage["text"] == "bcdefghi"
    assert passage["source_window"] == {"start": 1, "end": 9, "total": 10}
