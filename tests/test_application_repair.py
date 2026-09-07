import pytest

from openkb.application.pages import read_page, save_page
from openkb.mutation import RecoveryRequired, repair_marker, snapshot_paths


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        (".env", "LLM_API_KEY='unterminated"),
        (".openkb/hashes.json", '{"legacy-hash": {"doc_name": ["not a name"], "type": "md"}}'),
    ],
)
def test_repair_retains_block_for_invalid_credentials_or_registry(kb_dir, filename, content):
    from openkb.application.repair import repair_knowledge_base

    (kb_dir / filename).write_text(content)
    repair_marker(kb_dir).write_text("{}")
    result = repair_knowledge_base(kb_dir)
    assert not result.repaired
    assert repair_marker(kb_dir).exists()
    assert content not in str(result)


def test_stale_creation_intent_cannot_bypass_existing_storage_checks(kb_dir):
    import json

    from openkb.application.repair import repair_knowledge_base

    (kb_dir / ".openkb/initializing.json").write_text(
        json.dumps({"version": 1, "kb_dir": str(kb_dir)})
    )
    (kb_dir / ".openkb/config.yaml").unlink()
    repair_marker(kb_dir).write_text("{}")
    assert not repair_knowledge_base(kb_dir).repaired
    assert repair_marker(kb_dir).exists()


def test_repair_retains_missing_backup_and_resumes_only_after_checked_recovery(kb_dir):
    from openkb.application.repair import repair_knowledge_base

    page = kb_dir / "wiki/concepts/attention.md"
    page.write_text("Original\n")
    snapshot = snapshot_paths(kb_dir, [page], operation="probe")
    backup = snapshot.entries[page.resolve()]
    backup.unlink()
    page.write_text("Incomplete mutation\n")
    with pytest.raises(RecoveryRequired):
        read_page(kb_dir, "concepts/attention")
    failed = repair_knowledge_base(kb_dir)
    assert not failed.repaired
    assert snapshot.journal_path.exists()
    assert repair_marker(kb_dir).exists()
    assert page.read_text() == "Incomplete mutation\n"

    # A human restores the missing evidence. The controlled operation can now
    # validate it and finish the original rollback, without guessing content.
    backup.write_text("Original\n")
    result = repair_knowledge_base(kb_dir)
    assert result.repaired
    assert not repair_marker(kb_dir).exists()
    assert read_page(kb_dir, "concepts/attention").body == "Original\n"
    assert save_page(kb_dir, "concepts/attention", "Checked\n").status == "saved"
