"""Page behavior at the confirmed application use-case boundary."""

import pytest

from ultrakb.application.pages import read_page, save_page


def test_editor_retains_metadata_and_rejects_a_stale_draft(kb_dir):
    page = kb_dir / "wiki/concepts/attention.md"
    page.write_text("---\ntype: Concept\ndescription: 注意力\n---\nOriginal\n")
    from publication_read_inputs import publish_fixture

    publish_fixture(kb_dir)
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
    from publication_read_inputs import publish_fixture

    publish_fixture(kb_dir)
    opened = read_page(kb_dir, "concepts/attention")
    import json

    from ultrakb.storage.database import open_database

    with open_database(kb_dir) as db, db.transaction() as connection:
        connection.execute(
            "UPDATE ukb_meta SET required_capabilities = ?", (json.dumps(["unknown-writer"]),)
        )
    for _ in range(2):
        with pytest.raises(ValueError, match="Unsupported knowledge-base storage"):
            save_page(kb_dir, "concepts/attention", "Replacement\n", version=opened.version)
        assert page.read_text() == "Original\n"


def test_create_and_reopen_existing_knowledge_base_without_changing_cwd(tmp_path, monkeypatch):
    from ultrakb import config
    from ultrakb.application.knowledge_bases import get_kb_list, initialize_kb, open_kb

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    kb = tmp_path / "知识库"
    initialize_kb(kb, model="test-model", language="zh", seed_environment=False)
    assert open_kb(kb) == kb.resolve()
    assert get_kb_list(kb)["document_count"] == 0
    assert config.resolve_effective_config(kb)[0]["language"] == "zh"
    assert not (kb / ".env").exists()


def test_open_rejects_invalid_runtime_fields_before_registering(kb_dir, tmp_path, monkeypatch):
    from ultrakb import config
    from ultrakb.application.knowledge_bases import open_kb

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    (kb_dir / ".ultrakb/config.yaml").write_text("model: []\nlanguage: {}\n")
    with pytest.raises(ValueError, match="model.*string"):
        open_kb(kb_dir)
    assert not config.GLOBAL_CONFIG_PATH.exists()


def test_open_accepts_legacy_zero_threshold_after_settings_save(kb_dir, tmp_path, monkeypatch):
    from ultrakb import config
    from ultrakb.application.knowledge_bases import open_kb
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb="test", config={"pageindex_threshold": 0, "model": ""})
    )
    assert open_kb(kb_dir) == kb_dir.resolve()


@pytest.mark.parametrize("changed", ["first", "second"])
def test_page_deletion_rejects_a_stale_publication_or_page_version(kb_dir, changed):
    from publication_read_inputs import publish_fixture

    from ultrakb.application.pages import delete_page
    from ultrakb.application.views import published_scope

    target = kb_dir / "wiki/concepts/first.md"
    target.write_text("First body.\n")
    (kb_dir / "wiki/concepts/second.md").write_text("See [[concepts/first]].\n")
    publish_fixture(kb_dir)
    opened = delete_page(kb_dir, "concepts/first", dry_run=True)
    assert opened["status"] == "dry_run"
    assert opened["backlinks"] == ["concepts/second"]
    assert opened["publication_id"] == published_scope(kb_dir).publication_id

    save_page(kb_dir, f"concepts/{changed}", "A concurrent edit.\n")
    current = published_scope(kb_dir)
    before = target.read_bytes()
    stale = delete_page(
        kb_dir,
        "concepts/first",
        publication_id=opened["publication_id"],
        version=opened["version"],
    )
    assert stale["status"] == "conflict"
    assert target.read_bytes() == before
    assert published_scope(kb_dir).publication_id == current.publication_id

    wrong_hash = delete_page(
        kb_dir,
        "concepts/first",
        publication_id=current.publication_id,
        version="a different manual baseline",
    )
    assert wrong_hash["status"] == "conflict"
    assert target.read_bytes() == before

    confirmed = delete_page(kb_dir, "concepts/first", dry_run=True)
    deleted = delete_page(
        kb_dir,
        "concepts/first",
        version=confirmed["version"],
        publication_id=confirmed["publication_id"],
    )
    assert deleted["status"] == "deleted"
    assert not target.exists()
    assert published_scope(kb_dir).publication_id != current.publication_id


def test_page_deletion_rejects_an_old_generation_without_writing(kb_dir):
    from publication_read_inputs import publish_fixture

    from ultrakb.application.pages import delete_page

    target = kb_dir / "wiki/concepts/first.md"
    target.write_text("Retain this page.\n")
    publish_fixture(kb_dir)
    opened = delete_page(kb_dir, "concepts/first", dry_run=True)
    previous = kb_dir.with_name(kb_dir.name + "-previous")
    kb_dir.rename(previous)
    from ultrakb.application.knowledge_bases import initialize_kb

    initialize_kb(kb_dir, seed_environment=False)
    from ultrakb.desktop.verification_data import publish_pages

    publish_pages(kb_dir, {"concepts/first.md": b"Retain this page.\n"})

    result = delete_page(
        kb_dir,
        "concepts/first",
        version=opened["version"],
        publication_id=opened["publication_id"],
    )
    assert result["status"] == "conflict"
    assert target.read_text() == "Retain this page.\n"


def test_page_deletion_rechecks_public_identity_before_its_commit(kb_dir, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from publication_read_inputs import publish_fixture

    from ultrakb.application.pages import delete_page
    from ultrakb.storage.knowledge_store import KnowledgeStore

    target = kb_dir / "wiki/concepts/first.md"
    target.write_text("Retain this page.\n")
    (kb_dir / "wiki/concepts/second.md").write_text("Other page.\n")
    publish_fixture(kb_dir)
    opened = delete_page(kb_dir, "concepts/first", dry_run=True)
    prepared, release = threading.Event(), threading.Event()
    publish = KnowledgeStore.publish
    owner = []

    def competing_publish(store, revision, **kwargs):
        if owner and threading.get_ident() == owner[0]:
            prepared.set()
            assert release.wait(5)
        return publish(store, revision, **kwargs)

    monkeypatch.setattr(KnowledgeStore, "publish", competing_publish)

    def delete():
        owner.append(threading.get_ident())
        return delete_page(
            kb_dir,
            "concepts/first",
            version=opened["version"],
            publication_id=opened["publication_id"],
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(delete)
        try:
            assert prepared.wait(5)
            assert (
                save_page(kb_dir, "concepts/second", "Concurrent public knowledge.\n").status
                == "saved"
            )
        finally:
            release.set()
        assert pending.result(5)["status"] == "conflict"
    assert target.read_text() == "Retain this page.\n"
