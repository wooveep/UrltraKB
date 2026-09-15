"""Concurrent settings reads retain transaction and recovery guarantees."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from openkb import config
from openkb.application import settings
from openkb.application.settings_data import KbConfigPatchRequest
from openkb.locks import atomic_write_text, kb_ingest_lock, kb_read_lock
from openkb.mutation import RecoveryRequired, repair_marker, snapshot_paths
from openkb.settings_access import settings_read_lock


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global/global.yaml")


def test_read_waits_for_complete_settings_pair(kb_dir, monkeypatch):
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    write_env = settings._merge_patch_env

    def paused_write(*args, **kwargs):
        entered.set()  # config.yaml changed, but .env has not changed yet.
        assert release.wait(5)
        return write_env(*args, **kwargs)

    def read():
        with settings_read_lock(kb_dir, on_wait=waiting.set):
            return settings.read_settings_view(kb_dir)

    monkeypatch.setattr(settings, "_merge_patch_env", paused_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(
            settings.apply_kb_config_patch,
            kb_dir,
            KbConfigPatchRequest(kb=str(kb_dir), config={"model": "new"}, api_key="test-secret"),
        )
        try:
            assert entered.wait(3)
            reader = pool.submit(read)
            assert waiting.wait(3)
            assert not reader.done()
        finally:
            release.set()
        writer.result(3)
        result = reader.result(3)
    assert result.values.model == "new" and result.values.has_api_key
    assert "test-secret" not in result.model_dump_json()


def interrupted_settings(kb):
    settings.apply_kb_config_patch(kb, KbConfigPatchRequest(kb=str(kb), config={"model": "old"}))
    with kb_ingest_lock(kb / ".openkb"):
        snapshot = snapshot_paths(
            kb, [kb / ".openkb/config.yaml", kb / ".env"], operation="settings"
        )
        atomic_write_text(kb / ".openkb/config.yaml", "model: partial\n")
        atomic_write_text(kb / ".env", "LLM_API_KEY=interrupted-secret\n")
    return snapshot


def test_read_recovers_interrupted_settings_without_exposing_partial_values(kb_dir):
    snapshot = interrupted_settings(kb_dir)
    result = settings.read_settings_view(kb_dir)
    assert result.values.model == "old"
    assert result.sources["api_key"] != "kb"
    assert "interrupted-secret" not in result.model_dump_json()
    assert not snapshot.journal_path.exists()


def test_read_waits_for_complete_recovery_pair(kb_dir, monkeypatch):
    from openkb import mutation

    interrupted_settings(kb_dir)
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    copy = mutation._copy_file_atomic

    def paused_restore(source, target, **kwargs):
        copy(source, target, **kwargs)
        if target == kb_dir / ".openkb/config.yaml":
            entered.set()  # Restored config, but credentials are still partial.
            assert release.wait(5)

    def recover():
        with kb_read_lock(kb_dir / ".openkb"):
            pass

    def read():
        with settings_read_lock(kb_dir, on_wait=waiting.set):
            return settings.read_settings_view(kb_dir)

    monkeypatch.setattr(mutation, "_copy_file_atomic", paused_restore)
    with ThreadPoolExecutor(max_workers=2) as pool:
        recovery = pool.submit(recover)
        try:
            assert entered.wait(3)
            reader = pool.submit(read)
            assert waiting.wait(3)
            assert not reader.done()
        finally:
            release.set()
        recovery.result(3)
        result = reader.result(3)
    assert result.values.model == "old"
    assert result.sources["api_key"] != "kb"


def test_read_preserves_explicit_repair_gate(kb_dir):
    marker = repair_marker(kb_dir)
    atomic_write_text(marker, "{}")
    with pytest.raises(RecoveryRequired):
        settings.read_settings_view(kb_dir)
    assert marker.exists()
