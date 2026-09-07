"""Settings behavior shared by local adapters, using real KB files."""

import pytest


def test_initialization_distinguishes_owned_creation_lock_from_existing_kb(tmp_path, monkeypatch):
    from openkb import config
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.locks import kb_ingest_lock

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    kb = tmp_path / "created"
    with kb_ingest_lock(kb / ".openkb"):
        with config._with_global_config_lock():
            assert initialize_kb(kb, seed_environment=False)["created"]
        with pytest.raises(FileExistsError):
            initialize_kb(kb, seed_environment=False)
    assert (kb / ".openkb/config.yaml").is_file()
    incomplete = tmp_path / "incomplete/.openkb"
    incomplete.mkdir(parents=True)
    (incomplete / "unexpected").write_text("keep")
    with kb_ingest_lock(incomplete):
        with pytest.raises(FileExistsError):
            initialize_kb(incomplete.parent)
    assert (incomplete / "unexpected").read_text() == "keep"


def test_global_settings_restore_both_files_after_write_failure(tmp_path, monkeypatch):
    from openkb import config
    from openkb.application import settings
    from openkb.application.settings_data import GlobalConfigPatchRequest

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global.yaml")
    settings.apply_global_config_patch(
        GlobalConfigPatchRequest(config={"model": "before"}, api_key="before-secret")
    )
    before = {path: path.read_bytes() for path in (tmp_path / "global.yaml", tmp_path / ".env")}
    write = settings._write_global_env

    def fail_after_write(*args):
        write(*args)
        raise OSError("simulated disk failure")

    monkeypatch.setattr(settings, "_write_global_env", fail_after_write)
    with pytest.raises(OSError, match="disk failure"):
        settings.apply_global_config_patch(
            GlobalConfigPatchRequest(config={"model": "after"}, api_key="after-secret")
        )
    assert {path: path.read_bytes() for path in before} == before


def test_global_settings_read_recovers_an_interrupted_pair(tmp_path, monkeypatch):
    from openkb import config
    from openkb.application import settings
    from openkb.application.settings_data import GlobalConfigPatchRequest
    from openkb.locks import atomic_write_text
    from openkb.mutation import snapshot_paths

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global.yaml")
    settings.apply_global_config_patch(
        GlobalConfigPatchRequest(config={"model": "before"}, api_key="before-secret")
    )
    with config._with_global_config_lock():
        snapshot_paths(
            tmp_path,
            [tmp_path / "global.yaml", tmp_path / ".env"],
            operation="global-settings",
        )
        atomic_write_text(tmp_path / "global.yaml", "model: interrupted\n")
    result = settings.read_global_config()
    assert result.model == "before" and result.has_api_key
    assert not list((tmp_path / ".openkb/journal").glob("*.json"))


def test_credential_snapshot_and_recovery_preserve_private_permissions(tmp_path):
    import os
    import stat

    from openkb.mutation import snapshot_paths

    if os.name == "nt":
        pytest.skip("POSIX permission bits; Windows inherits the profile's ACL")
    credential = tmp_path / ".env"
    credential.write_text("LLM_API_KEY=private-fixture\n")
    credential.chmod(0o600)
    snapshot = snapshot_paths(tmp_path, [credential], operation="settings")
    backup = snapshot.entries[credential]
    assert backup is not None
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(snapshot.backup_dir.stat().st_mode) == 0o700
    credential.unlink()
    snapshot.rollback()
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    snapshot.discard()


def test_global_repair_retains_bad_evidence_then_allows_controlled_retry(tmp_path, monkeypatch):
    from openkb import config
    from openkb.application.repair import repair_global_settings
    from openkb.application.settings import read_global_config
    from openkb.locks import atomic_write_text
    from openkb.mutation import RecoveryRequired, repair_marker, snapshot_paths

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global.yaml")
    config.save_global_config({"model": "before"})
    with config._with_global_config_lock():
        snapshot = snapshot_paths(
            tmp_path, [config.GLOBAL_CONFIG_PATH], operation="global-settings"
        )
        backup = snapshot.entries[config.GLOBAL_CONFIG_PATH]
        assert backup is not None
        previous = backup.read_text()
        backup.unlink()
        atomic_write_text(config.GLOBAL_CONFIG_PATH, "model: interrupted\n")
    with pytest.raises(RecoveryRequired):
        read_global_config()
    assert not repair_global_settings().repaired
    assert snapshot.journal_path.exists() and repair_marker(tmp_path).exists()
    atomic_write_text(backup, previous)
    assert repair_global_settings().repaired
    assert read_global_config().model == "before"
    assert not repair_marker(tmp_path).exists()


def test_rest_config_read_does_not_block_event_loop_on_global_lock(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time

    from openkb import config
    from openkb.api_config_router import global_config_get

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global.yaml")
    locked, release = threading.Event(), threading.Event()

    def hold():
        with config._with_global_config_lock():
            locked.set()
            release.wait(2)

    holder = threading.Thread(target=hold)
    holder.start()
    assert locked.wait(2)

    async def read():
        asyncio.get_running_loop().call_later(0.05, release.set)
        started = time.monotonic()
        await global_config_get()
        assert time.monotonic() - started < 1

    try:
        asyncio.run(read())
    finally:
        release.set()
        holder.join(3)


def test_invalid_credential_patch_does_not_partially_save_model(kb_dir):
    from openkb.application.settings import apply_kb_config_patch, read_kb_config
    from openkb.application.settings_data import KbConfigPatchRequest

    previous = read_kb_config(kb_dir)
    before = (kb_dir / ".openkb/config.yaml").read_text()
    patch = KbConfigPatchRequest(kb=str(kb_dir), config={"model": "new"}, api_key="bad\nkey")
    with pytest.raises(ValueError, match="newline"):
        apply_kb_config_patch(kb_dir, patch)
    assert read_kb_config(kb_dir) == previous
    assert (kb_dir / ".openkb/config.yaml").read_text() == before
    assert not (kb_dir / ".env").exists()


def test_settings_patch_preserves_three_states_and_hides_secret(kb_dir, tmp_path, monkeypatch):
    from openkb import config
    from openkb.application.settings import apply_kb_config_patch, read_kb_config
    from openkb.application.settings_data import KbConfigPatchRequest

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    config.save_global_config({"model": "inherited"})
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb="kb", api_key="private-test-key"))
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb="kb", config={"model": "override"}))
    current = read_kb_config(kb_dir)
    assert current.model == "override"
    assert current.sources["model"] == "kb"
    assert current.has_api_key
    assert "private-test-key" not in current.model_dump_json()
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb="kb", config={"model": None}))
    current = read_kb_config(kb_dir)
    assert current.model == "inherited"
    assert current.sources["model"] == "global"
    assert current.has_api_key
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb="kb", api_key=None))
    assert "LLM_API_KEY" not in (kb_dir / ".env").read_text()
