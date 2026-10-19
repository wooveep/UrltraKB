"""Public maintenance protects unknown recovery ownership and active backup reads."""

import json

import pytest
from test_storage_questions import question_knowledge

from ultrakb.application.views import storage_scope


@pytest.mark.parametrize("packet", ["malformed", "unknown_request"])
def test_unknown_pending_ownership_blocks_collection_without_losing_unreferenced_bytes(
    tmp_path, packet
):
    from ultrakb.application.storage_maintenance import collect_garbage, preview_collection
    from ultrakb.storage.blobs import blob_path, put_blob
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.receipt_store import ReceiptStore

    root, identity, _ = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    with storage_stage(root, identity) as db:
        orphan = put_blob(db, b"Potentially owned by an unconfirmed recovery packet")
    pending = ReceiptStore(root, identity).pending
    pending.mkdir(parents=True)
    path = pending / "unknown.json"
    path.write_text(
        "Unrecognized packet"
        if packet == "malformed"
        else json.dumps(
            {
                "schema_version": 1,
                "kb_id": identity.kb_id,
                "generation": identity.generation,
                "request_id": "0" * 32,
                "version": 1,
                "response_base64": None,
                "usage": {},
            }
        )
    )
    preview = preview_collection(root, scope=scope)
    assert preview.candidate_count == 0
    assert preview.protected_bytes >= orphan.byte_count
    assert preview.blocked_reason == "unresolved_pending_receipts"
    result = collect_garbage(root, scope=scope)
    assert result.status == "blocked"
    assert result.deleted_count == result.trees_deleted == 0
    assert result.blocked_reason == "unresolved_pending_receipts"
    assert blob_path(root, orphan.digest).read_bytes().startswith(b"Potentially owned")
    path.unlink()
    assert collect_garbage(root, scope=scope).status == "completed"
    assert not blob_path(root, orphan.digest).exists()


def test_backup_protects_snapshot_blobs_for_the_entire_actual_copy_interval(tmp_path, monkeypatch):
    from ultrakb.application.storage_maintenance import backup_knowledge, collect_garbage
    from ultrakb.storage import maintenance

    root, identity, _ = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    copy = maintenance._copy_verified
    observed = []

    def while_copying(source, target, digest):
        if source.is_relative_to(root) and "blobs" in source.parts:
            result = collect_garbage(root, scope=scope)
            observed.append(result.status)
            assert result.deleted_count == result.trees_deleted == 0
        return copy(source, target, digest)

    monkeypatch.setattr(maintenance, "_copy_verified", while_copying)
    backup_knowledge(root, tmp_path / "backup", scope=scope)
    assert observed and set(observed) == {"busy"}
