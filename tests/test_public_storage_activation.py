"""Public factories and all adapters select only the authoritative v2 store."""

import asyncio
import json
import multiprocessing
import shutil
import time

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from stream_transport import StreamTransport

from ultrakb.application.knowledge_bases import get_kb_list, initialize_kb, open_kb
from ultrakb.application.views import published_scope, view_scope
from ultrakb.config import register_kb_alias, save_config
from ultrakb.knowledge_scope import resolve_scope
from ultrakb.storage.database import open_database


def _question_worker(request, token, channel):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.runtime.worker import _execute

    try:
        channel.send(_execute(request, token, ExecutionContext()))
    finally:
        channel.close()


def test_public_creation_publishes_a_complete_v2_store_and_reads_without_the_projection(tmp_path):
    root = tmp_path / "public"
    result = initialize_kb(root, seed_environment=False, require_empty=True)
    assert result["created"]
    with open_database(root, read_only=True) as db:
        identity = db.identity
    selected = resolve_scope(root)
    assert selected.knowledge.identity == identity
    assert published_scope(root).publication_id == view_scope(root, "legacy").publication_id
    assert selected.knowledge.read("AGENTS.md") and selected.knowledge.read("index.md")
    for name in ("hashes.json", "format.json", "published", "knowledge", "usage", "model-tasks"):
        assert not (root / ".ultrakb" / name).exists()
    shutil.rmtree(root / "wiki")
    assert open_kb(root) == root and not (root / "wiki").exists()
    assert get_kb_list(root)["document_count"] == 0


def test_cli_import_http_question_and_native_worker_share_the_public_main_store(
    tmp_path, monkeypatch
):
    from ultrakb.api import create_app
    from ultrakb.application.conversations import read_conversation
    from ultrakb.cli import cli
    from ultrakb.runtime.records import UnitIdentity
    from ultrakb.runtime.requests import ContinueConversation
    from ultrakb.storage.usage_store import UsageStore

    root = tmp_path / "entrypoints"
    initialize_kb(root, seed_environment=False)
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 8, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
            "role_generation": {"answer": {"max_tokens": 128}},
        },
    )
    source = tmp_path / "fixture.md"
    source.write_text("The retained public value is 17.")
    responses = [
        {
            "content": json.dumps(
                {"description": "Fixture", "content": "The retained public value is 17."}
            )
        },
        {"content": json.dumps({"concepts": [], "entities": []})},
        {"content": "The public value is 17."},
        {"content": "The original answer is 17."},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        imported = CliRunner().invoke(cli, ["--kb-dir", str(root), "add", str(source)])
        assert imported.exit_code == 0, imported.output
        assert get_kb_list(root)["document_count"] == 1
        register_kb_alias("public-native", root)
        monkeypatch.setenv("ULTRAKB_API_TOKEN", "offline-native-token")
        with TestClient(create_app()) as client:
            answer = client.post(
                "/api/v1/query",
                json={"kb": "public-native", "question": "Read the value.", "stream": False},
                headers={"Authorization": "Bearer offline-native-token"},
            )
            assert answer.status_code == 200, answer.text
            assert answer.json()["answer"] == "The public value is 17."
        request = ContinueConversation("Read the original value.", new_session_id="public-worker")
        selected = resolve_scope(root)
        token = UnitIdentity.create(
            "a" * 32, 0, str(root), request, generation=selected.knowledge.identity.generation
        )
        spawn = multiprocessing.get_context("spawn")
        parent, child = spawn.Pipe(duplex=False)
        worker = spawn.Process(target=_question_worker, args=(request, token, child))
        worker.start()
        child.close()
        try:
            assert parent.poll(15), "The native question worker did not settle"
            completed = parent.recv()
        finally:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
            parent.close()
        assert worker.exitcode == 0
        assert completed.status == "completed", completed.error
        assert read_conversation(root, "public-worker").turns == (
            ("Read the original value.", "The original answer is 17."),
        )
        assert len(service.requests) == 4
    source_id = get_kb_list(root)["documents"][0]["source_id"]
    assert (
        UsageStore(root, selected.knowledge.identity).summary(source_id=source_id)["actual_sends"]
        == 2
    )
    assert answer.json()["model_usage"]["current"]["actual_sends"] == 1
    assert completed.model_usage["current"]["actual_sends"] == 1
    for name in (
        "hashes.json",
        "format.json",
        "published",
        "knowledge",
        "usage",
        "chats",
        "model-tasks",
        "context.sqlite",
    ):
        assert not (root / ".ultrakb" / name).exists()


def test_old_configured_directories_are_rejected_before_creation_or_execution(tmp_path):
    from ultrakb.application.conversations import ask_question

    root = tmp_path / "old"
    (root / ".ultrakb").mkdir(parents=True)
    (root / ".ultrakb/config.yaml").write_text("language: en\n")
    (root / ".ultrakb/format.json").write_text('{"format":"ultrakb.context-kb","version":1}')
    (root / "wiki").mkdir()
    (root / "wiki/index.md").write_text("Original user content")
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    with pytest.raises((ValueError, FileExistsError)):
        initialize_kb(root)
    with pytest.raises(ValueError):
        open_kb(root)
    with pytest.raises(ValueError):
        asyncio.run(ask_question(root, "Question."))
    assert before == {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_qt_creation_adapter_does_not_precreate_a_legacy_lock_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from ultrakb.desktop.io import LocalIO

    app = widgets.QApplication.instance() or widgets.QApplication([])
    io = LocalIO()
    root = tmp_path / "qt-created"
    received = []
    io.submit(
        lambda: initialize_kb(root, seed_environment=False, require_empty=True),
        lambda result, error: received.append((result, error)),
        kb=root,
        creating=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert received and received[0][1] is None, received
        assert resolve_scope(root).knowledge.read("index.md")
        assert (root / ".ultrakb/kb.sqlite").is_file()
        assert not (root / ".ultrakb/format.json").exists()
    finally:
        io.stop()
        deadline = time.monotonic() + 5
        while not io.stopped() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert io.stopped()


def test_qt_queued_read_rejects_database_replacement_at_the_same_directory(tmp_path, monkeypatch):
    import os
    from threading import Event

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from ultrakb.desktop.io import LocalIO

    app = widgets.QApplication.instance() or widgets.QApplication([])
    root, replacement = tmp_path / "kb", tmp_path / "replacement"
    initialize_kb(root, seed_environment=False)
    initialize_kb(replacement, seed_environment=False)
    io = LocalIO()
    release, ready = Event(), [Event() for _ in range(4)]
    reads, received = [], []

    def occupy(started):
        started.set()
        assert release.wait(5)

    blockers = [io._pool.submit(occupy, started) for started in ready]
    try:
        assert all(started.wait(3) for started in ready)
        io.submit(
            lambda: reads.append("read"), lambda value, error: received.append(error), kb=root
        )
        os.replace(root / ".ultrakb", tmp_path / "old-state")
        os.replace(replacement / ".ultrakb", root / ".ultrakb")
        release.set()
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert received and isinstance(received[0], ValueError), received
        assert reads == []
    finally:
        release.set()
        for future in blockers:
            future.result(timeout=5)
        io.stop()
        deadline = time.monotonic() + 5
        while not io.stopped() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert io.stopped()


def test_delayed_page_render_checks_the_captured_native_database(tmp_path):
    import os
    from types import SimpleNamespace

    from ultrakb.application.pages import read_page
    from ultrakb.desktop.knowledge_loading import display_context

    root, replacement = tmp_path / "kb", tmp_path / "replacement"
    initialize_kb(root, seed_environment=False)
    initialize_kb(replacement, seed_environment=False)
    captured = {}
    scope = resolve_scope(root)
    window = SimpleNamespace(
        kb=root,
        _page_request_id=1,
        _open_request_id=2,
        _deleting_kbs=set(),
        reader=SimpleNamespace(show_markdown=lambda *args, **options: captured.update(options)),
    )
    display_context(window, SimpleNamespace(page=read_page(root, "index.md", scope=scope)), scope)
    assert captured["is_current"]()
    os.replace(root / ".ultrakb", tmp_path / "old-state")
    os.replace(replacement / ".ultrakb", root / ".ultrakb")
    assert not captured["is_current"]()


def test_real_qt_workbench_opens_native_knowledge_and_keeps_the_full_conversation_count(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap

    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "workbench"
    initialize_kb(root, seed_environment=False)
    identity = resolve_scope(root).knowledge.identity
    sessions = SessionStore(root, identity)
    sessions.create("long-desktop", model="gpt-4o-mini", language="en")
    for ordinal in range(70):
        sessions.append_exchange("long-desktop", str(ordinal), f"Q{ordinal}", f"A{ordinal}")
    program = tmp_path / "native_workbench.py"
    program.write_text(
        textwrap.dedent("""\
        import sys
        import logging
        import time
        from pathlib import Path
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        from ultrakb.desktop.window import Workbench

        if __name__ == "__main__":
            logging.getLogger().setLevel(logging.WARNING)
            root = Path(sys.argv[1])
            QSettings.setDefaultFormat(QSettings.Format.IniFormat)
            QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                              str(root.parent / "qt-settings"))
            app = QApplication([])
            window = Workbench(history_dir=root.parent / "tasks")
            errors = []
            window._error = lambda error: errors.append(error) if error is not None else None
            window.show()
            try:
                window.open_knowledge_base(root)
                deadline = time.monotonic() + 15
                while window.kb != root and not errors and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
                assert window.kb == root and not errors, errors
                assert window.scope.knowledge.read("index.md")
                window.conversations.open("long-desktop")
                while (window.conversations.active.loading and not errors
                       and time.monotonic() < deadline):
                    app.processEvents()
                    time.sleep(0.01)
                chat = window.conversations.active
                assert not chat.loading and not chat.read_error and not errors, errors
                assert chat.completed_count == 70, chat.completed_count
                assert chat.turns[-1] == ("Q69", "A69")
                assert not (root / ".ultrakb/chats").exists()
                print("native workbench and complete count verified")
            finally:
                window.request_quit()
                deadline = time.monotonic() + 5
                while not window.shutdown_complete() and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
                assert window.shutdown_complete()
    """)
    )
    environment = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "QTWEBENGINE_DISABLE_SANDBOX": "1",
        "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu --disable-dev-shm-usage",
    }
    completed = subprocess.run(
        [sys.executable, str(program), str(root)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    assert "native workbench and complete count verified" in completed.stdout
