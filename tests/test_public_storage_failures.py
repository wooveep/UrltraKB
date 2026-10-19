"""Public activation preserves committed facts across auxiliary failures."""

import json
import sqlite3
from contextlib import contextmanager
from threading import Event, Thread

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.pages import read_page
from ultrakb.application.recompilation import recompile_document
from ultrakb.config import LlmCredentialBundle
from ultrakb.knowledge_scope import resolve_scope
from ultrakb.runtime.records import UnitIdentity
from ultrakb.runtime.requests import SavePage
from ultrakb.runtime.worker import _execute
from ultrakb.storage.database import open_database
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.wiki_projection import ProjectionStore


def test_creation_reports_a_failed_sql_projection_without_losing_the_installed_seed(
    tmp_path, monkeypatch
):
    from ultrakb.storage import wiki_projection

    root = tmp_path / "created"
    stage = wiki_projection.storage_stage

    @contextmanager
    def denied_projection(*args, **kwargs):
        with stage(*args, **kwargs) as db:

            def authorize(action, table, *unused):
                return (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and table == "ukb_projection_state"
                    else sqlite3.SQLITE_OK
                )

            db.connection.set_authorizer(authorize)
            try:
                yield db
            finally:
                db.connection.set_authorizer(None)

    with monkeypatch.context() as fault:
        fault.setattr(wiki_projection, "storage_stage", denied_projection)
        result = initialize_kb(root, seed_environment=False)
    assert result["created"] and result["unfinished"] == ("synchronize wiki",)
    selected = resolve_scope(root)
    assert selected.knowledge.read("AGENTS.md")
    assert ProjectionStore(root, selected.knowledge.identity).sync().status == "synced"


def test_creation_reports_invalid_registration_after_committing_the_new_store(tmp_path):
    from ultrakb.config import GLOBAL_CONFIG_PATH

    GLOBAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_CONFIG_PATH.write_text("known_kbs: null\n")
    root = tmp_path / "created"
    result = initialize_kb(root, seed_environment=False)
    assert result["created"] and result["unfinished"] == ("register knowledge base",)
    assert resolve_scope(root).knowledge.read("index.md")


def test_creation_flushes_new_ancestor_bindings_before_installation(tmp_path, monkeypatch):
    from ultrakb import locks

    root = tmp_path / "new-parent" / "child" / "created"
    flush, flushed = locks._fsync_directory, []

    def tracked(directory):
        flush(directory)
        flushed.append(directory)

    monkeypatch.setattr(locks, "_fsync_directory", tracked)
    initialize_kb(root, seed_environment=False)
    assert all(path in flushed for path in (root, root.parent, root.parent.parent, tmp_path))


def test_native_page_save_keeps_committed_page_when_execution_confirmation_fails(tmp_path):
    root = tmp_path / "edit"
    initialize_kb(root, seed_environment=False)
    selected = resolve_scope(root)
    identity = selected.knowledge.identity
    store = KnowledgeStore(root, identity)
    store.publish(store.prepare_changes(selected.knowledge, {"concepts/edit.md": b"# Original\n"}))
    opened = read_page(root, "concepts/edit")
    request = SavePage("concepts/edit", "# Saved without model settings\n", opened.version)
    with open_database(root, expected=identity) as db:
        db.connection.execute(
            "CREATE TRIGGER deny_execution_finish BEFORE UPDATE OF state ON ukb_executions "
            "WHEN NEW.state = 'completed' BEGIN SELECT RAISE(FAIL, 'injected cleanup failure'); END"
        )
    token = UnitIdentity.create("a" * 32, 0, str(root), request, generation=identity.generation)
    result = _execute(request, token, ExecutionContext())
    assert result.status == "completed" and result.page is not None
    assert result.resources and result.revision == result.page.version
    assert "confirm execution cleanup" in result.unfinished
    assert read_page(root, "concepts/edit").body == result.page.body


def test_worker_scope_selection_can_stop_while_reclamation_is_still_held(tmp_path):
    from ultrakb.locks import LockCancelled
    from ultrakb.storage.coordination import try_reclamation

    root = tmp_path / "cancel"
    initialize_kb(root, seed_environment=False)
    opened = read_page(root, "index")
    identity = resolve_scope(root).knowledge.identity
    request = SavePage("index", "Never saved", opened.version)
    token = UnitIdentity.create("b" * 32, 0, str(root), request, generation=identity.generation)
    stopped, finished = Event(), Event()
    errors = []

    def worker():
        try:
            _execute(request, token, ExecutionContext(cancelled=stopped.is_set))
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    thread = Thread(target=worker)
    with try_reclamation(root, identity) as held:
        assert held is not None
        thread.start()
        try:
            assert not finished.wait(0.15)
            stopped.set()
            assert finished.wait(2)
            assert len(errors) == 1 and isinstance(errors[0], LockCancelled)
        finally:
            stopped.set()
    thread.join(2)
    assert not thread.is_alive()
    assert read_page(root, "index").version == opened.version


@pytest.mark.asyncio
async def test_recompile_uses_explicit_credentials_and_rejects_a_different_frozen_bundle(tmp_path):
    root, _, unit = compile_input(tmp_path)
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    with (
        StreamTransport([]) as default,
        StreamTransport(
            [{"content": summary}, {"content": json.dumps({"concepts": [], "entities": []})}]
        ) as explicit,
    ):
        (root / ".env").write_text(
            f"LLM_API_KEY=offline-default-key\nOPENAI_API_BASE={default.url}\n"
        )
        bundle = LlmCredentialBundle(api_key="offline-explicit-key", base_url=explicit.url)
        context = ExecutionContext()
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, context=context, bundle=bundle
        )
        assert result.status == "compiled", result.message
        assert len(explicit.requests) == 2 and not default.requests
        with pytest.raises(ValueError, match="credentials differ"):
            await recompile_document(
                root,
                unit.source_id,
                unit_id=unit.unit_id,
                context=context,
                bundle=LlmCredentialBundle(api_key="another-key", base_url=default.url),
            )
        assert len(explicit.requests) == 2 and not default.requests


def test_unsupported_root_child_keeps_its_name_over_a_same_named_external_store(
    tmp_path, monkeypatch
):
    from ultrakb import config

    root = tmp_path / "root"
    local = root / "same-name"
    (local / ".ultrakb").mkdir(parents=True)
    (local / ".ultrakb/config.yaml").write_text("language: en\n")
    external = tmp_path / "external" / "same-name"
    initialize_kb(external, seed_environment=False)
    monkeypatch.setattr(config, "kb_root_dir", lambda: root)
    assert config.resolve_kb_alias("same-name") == local
    assert all(name != "same-name" for name, _ in config.registered_kbs())
    with pytest.raises(ValueError):
        resolve_scope(config.resolve_kb_alias("same-name"))


def test_upload_releases_its_admission_lease_before_actual_compiler_requests(tmp_path):
    import time

    from ultrakb.application.documents import import_document
    from ultrakb.application.uploads import published_input
    from ultrakb.locks import file_write_lock

    root, _, _ = compile_input(tmp_path)
    original = tmp_path / "incoming.md"
    original.write_text("The uploaded value is 23.")
    leases = []

    def respond(request, ordinal):
        deadline = time.monotonic() + 0.5
        with file_write_lock(
            root / ".ultrakb/ingest.lock", cancelled=lambda: time.monotonic() > deadline
        ):
            leases.append(True)
        return {
            "content": json.dumps(
                {"description": "Upload", "content": "The uploaded value is 23."}
                if ordinal == 0
                else {"concepts": [], "entities": []}
            )
        }

    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-key\nOPENAI_API_BASE={service.url}\n")
        with published_input(root, original) as upload:
            result = import_document(root, upload.path)
            assert result.status == "added", result.message
            upload.discard_if_unregistered()
            assert upload.path.is_file()
        assert len(service.requests) == len(leases) == 2
