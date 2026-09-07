import pytest

from openkb.application.pages import read_page, save_page
from openkb.mutation import RecoveryRequired, repair_marker, snapshot_paths


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
