"""Actual SDK faults stop only their batch; completed/local work and receipts survive."""

import sys
from pathlib import Path

import pytest


@pytest.fixture
def provider(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from connection_probe_server import ProbeServer

    from ultrakb import config

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    server = ProbeServer()
    monkeypatch.setenv("LLM_API_KEY", "fixture-private-key")
    monkeypatch.setenv("OPENAI_API_BASE", server.url)
    yield server
    server.close()


@pytest.mark.parametrize("mode,code", [("authentication", "凭证"), ("quota", "额度")])
def test_sdk_shared_failure_halts_only_this_batch(kb_dir, tmp_path, provider, mode, code):
    from batch_retry_fixtures import retain_failure

    from ultrakb.application.document_actions import document_inventory
    from ultrakb.application.document_batches import preview_document_batch
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.pages import read_page
    from ultrakb.desktop.document_batch_actions import requests_for
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.runtime.records import TaskExclusion
    from ultrakb.runtime.requests import ImportFile, SavePage
    from ultrakb.runtime.tasks import TaskManager

    for index in range(2):
        source = kb_dir / f"raw/{index}.md"
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            atomic_write_text(source, f"# Source {index}\n\nSaved operating policy {index}.")
        retain_failure(kb_dir, source)
    # Restore the intentionally captured test transport after CLI fixture imports.
    import os

    os.environ["LLM_API_KEY"] = "fixture-private-key"
    os.environ["OPENAI_API_BASE"] = provider.url
    preview = preview_document_batch(
        kb_dir, tuple(row.identity for row in document_inventory(kb_dir)), action="retry"
    )
    requests = requests_for(preview, "legacy")
    assert len(requests) == 2
    other = tmp_path / "other"
    initialize_kb(other, seed_environment=False)
    with kb_ingest_lock(other / ".ultrakb"):
        atomic_write_text(other / "wiki/concepts/note.md", "# Note\n\nBefore.")
    from ultrakb.desktop.verification_data import publish_pages

    publish_pages(other, {"concepts/note.md": b"# Note\n\nBefore."})
    note = read_page(other, "concepts/note")
    provider.select(mode)
    manager = TaskManager(history_dir=tmp_path / "tasks")
    try:
        task = manager.submit(
            kb_dir,
            [ImportFile(str(tmp_path / "missing.md")), *requests],
            not_applicable=(TaskExclusion("legacy", "old", "旧资料", "没有未完成项。"),),
        )
        independent = manager.submit(
            other, [SavePage(note.path, "Independent saved body.", note.version)]
        )
        result = manager.wait(task, timeout=45)
        assert result.state == "blocked" and len(result.results) == 2, result
        assert not result.results[0].halt and result.results[1].halt
        assert code in result.results[1].error and "fixture-private-key" not in str(
            result.summary()
        )
        assert provider.requests, "The production SDK must reach the loopback failure boundary"
        assert manager.wait(independent).succeeded == 1
        assert "Independent" in read_page(other, note.path).body
        assert len(result.not_applicable) == 1 and result.processes_reaped
        assert not manager.has_work(kb_dir)
    finally:
        manager.shutdown(stop=True)
        assert manager.join(45)


def test_retry_revalidates_source_target_under_lease(kb_dir, tmp_path, provider):
    from batch_retry_fixtures import retain_failure

    from ultrakb.application.document_actions import document_inventory
    from ultrakb.application.document_batches import preview_document_batch
    from ultrakb.application.source_retry import RetryTargetChanged, retry_source
    from ultrakb.locks import atomic_write_text, kb_ingest_lock

    source = kb_dir / "raw/manual.md"
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(source, "# Source\n\nOriginal.")
    retain_failure(kb_dir, source)
    [row] = document_inventory(kb_dir)
    preview = preview_document_batch(kb_dir, (row.identity,), action="retry")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(source, "# Source\n\nReplacement.")
    retain_failure(kb_dir, source)
    with pytest.raises(RetryTargetChanged):
        retry_source(
            kb_dir,
            row.identity.value,
            source_revision_id=preview.retry_targets[0].source_revision_id,
        )
    assert not provider.requests


def test_explicit_stop_waits_for_inflight_sdk_cleanup_and_keeps_commit(kb_dir, tmp_path, provider):
    import time

    from ultrakb.application.pages import read_page
    from ultrakb.application.sources import source_inventory
    from ultrakb.desktop.verification_data import publish_pages
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.runtime.requests import ImportFile, SavePage
    from ultrakb.runtime.tasks import TaskManager

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(kb_dir / "wiki/concepts/note.md", "# Before\n\nOriginal body.")
        for name in ("first", "not-run"):
            atomic_write_text(kb_dir / f"raw/{name}.md", f"# {name}\n\nOperating policy.")
    publish_pages(kb_dir, {"concepts/note.md": b"# Before\n\nOriginal body."})
    note = read_page(kb_dir, "concepts/note")
    provider.select("wait")
    manager = TaskManager(history_dir=tmp_path / "stop-tasks")
    try:
        task = manager.submit(
            kb_dir,
            [
                SavePage(note.path, "Committed before stop.", note.version),
                ImportFile(str(kb_dir / "raw/first.md")),
                ImportFile(str(kb_dir / "raw/not-run.md")),
            ],
        )
        assert provider.started.wait(20)
        manager.stop(task)
        time.sleep(0.1)
        assert not manager.get(task).processes_reaped
        provider.release.set()
        result = manager.wait(task, timeout=25)
        assert result.state == "stopped" and result.processes_reaped
        assert result.results[0].status == "completed" and len(result.results) == 2
        assert "Committed" in read_page(kb_dir, note.path).body
        assert not any(
            source["location"].endswith("not-run.md") for source in source_inventory(kb_dir)
        )
    finally:
        provider.release.set()
        manager.shutdown(stop=True)
        assert manager.join(45)


def test_batch_reuses_initial_snapshot_after_global_settings_change(kb_dir, tmp_path, provider):
    from ultrakb.application.settings import apply_global_config_patch, apply_kb_config_patch
    from ultrakb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.runtime.requests import ImportFile
    from ultrakb.runtime.tasks import TaskManager

    apply_global_config_patch(GlobalConfigPatchRequest(config={"model": "gpt-5.4"}))
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"model": None}),
    )
    for name in ("first", "second"):
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            atomic_write_text(kb_dir / f"raw/{name}.md", f"# {name}\n\nOne operating policy.")
    provider.select("wait")
    manager = TaskManager(history_dir=tmp_path / "snapshot-tasks")
    try:
        task = manager.submit(
            kb_dir, [ImportFile(str(kb_dir / f"raw/{name}.md")) for name in ("first", "second")]
        )
        assert provider.started.wait(20)
        apply_global_config_patch(GlobalConfigPatchRequest(config={"model": "gpt-5.4-pro"}))
        provider.release.set()
        result = manager.wait(task, timeout=35)
        assert len(result.results) == 2
        assert len(provider.requests) >= 2
        assert {request["model"] for request in provider.requests} == {"gpt-5.4"}
    finally:
        provider.release.set()
        manager.shutdown(stop=True)
        assert manager.join(45)


def test_workbook_completed_sheet_survives_shared_failure_and_tail_stays_pending(kb_dir, tmp_path):
    from openpyxl import Workbook
    from test_storage_conversation_writes import StreamTransport

    from ultrakb.application.document_actions import document_inventory
    from ultrakb.application.document_batches import preview_document_batch
    from ultrakb.application.documents import import_document
    from ultrakb.application.source_retry import retry_source
    from ultrakb.config import load_config, save_config

    book = Workbook()
    book.active.title = "Alpha"
    book.active["A1"] = "Previously completed alpha data"
    for name in ("Beta", "Gamma"):
        book.create_sheet(name)["A1"] = f"Unfinished {name} operating policy"
    path = tmp_path / "partial.xlsx"
    book.save(path)
    save_config(
        kb_dir / ".ultrakb/config.yaml",
        {
            **load_config(kb_dir / ".ultrakb/config.yaml"),
            "model": "openai/gpt-4o-mini",
            "model_policy": {"max_calls": 8, "retries": 0},
        },
    )
    error = {
        "error": {"message": "Invalid key", "type": "invalid_api_key", "code": "invalid_api_key"}
    }
    with StreamTransport(
        [
            {"content": '{"description":"Alpha","content":"Previously completed alpha data"}'},
            {"content": '{"concepts":[],"entities":[]}'},
            error,
            error,
        ],
        statuses=[200, 200, 401, 401],
    ) as service:
        (kb_dir / ".env").write_text(f"LLM_API_KEY=offline-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(kb_dir, path)
        assert result.status == "partial", result.message
        assert len(service.requests) == 3
        retried = retry_source(kb_dir, result.source_id)
        assert retried.status == "partial", retried.message
        assert len(service.requests) == 4
    [row] = document_inventory(kb_dir)
    assert len(row.document["units"]) == 3
    assert {unit["name"]: unit["status"] for unit in row.document["units"]} == {
        "Alpha": "completed",
        "Beta": "failed",
        "Gamma": "pending",
    }
    preview = preview_document_batch(kb_dir, (row.identity,), action="retry")
    assert len(preview.retry_targets) == 2
    assert all("Alpha" not in target.name for target in preview.retry_targets)
