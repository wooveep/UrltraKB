"""Page behavior at the confirmed application use-case boundary."""

import pytest

from openkb.application.pages import read_page, save_page


def test_editor_retains_metadata_and_rejects_a_stale_draft(kb_dir):
    page = kb_dir / "wiki/concepts/attention.md"
    page.write_text("---\ntype: Concept\ndescription: 注意力\n---\nOriginal\n")
    opened = read_page(kb_dir, "concepts/attention")
    assert opened.body == "Original\n"

    saved = save_page(kb_dir, "concepts/attention", "Updated\n", version=opened.version)
    assert saved.status == "saved"
    assert read_page(kb_dir, "concepts/attention").content == (
        "---\ntype: Concept\ndescription: 注意力\n---\nUpdated\n"
    )
    conflict = save_page(kb_dir, "concepts/attention", "My draft\n", version=opened.version)
    assert conflict.status == "conflict"
    assert conflict.draft == "My draft\n"
    assert conflict.page.body == "Updated\n"


def test_corrupt_recovery_record_blocks_saving_and_retains_evidence(kb_dir):
    page = kb_dir / "wiki/concepts/attention.md"
    page.write_text("Original\n")
    opened = read_page(kb_dir, "concepts/attention")
    journal = kb_dir / ".openkb/journal/broken.json"
    journal.parent.mkdir()
    journal.write_text("{incomplete")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="needs repair"):
            save_page(kb_dir, "concepts/attention", "Replacement\n", version=opened.version)
        assert journal.read_text() == "{incomplete"
        assert page.read_text() == "Original\n"


def test_create_and_reopen_existing_knowledge_base_without_changing_cwd(tmp_path, monkeypatch):
    from openkb import config
    from openkb.application.knowledge_bases import get_kb_list, initialize_kb, open_kb

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    kb = tmp_path / "知识库"
    initialize_kb(kb, model="test-model", language="zh", seed_environment=False)
    assert open_kb(kb) == kb.resolve()
    assert get_kb_list(kb)["document_count"] == 0
    assert config.resolve_effective_config(kb)[0]["language"] == "zh"
    assert not (kb / ".env").exists()
