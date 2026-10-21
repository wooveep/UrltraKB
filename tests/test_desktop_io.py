"""Native local I/O keeps its original KB identity across deferred attempts."""

import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_local_io_retry_does_not_rebind_to_a_recreated_kb(kb_dir, monkeypatch):
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.desktop.io import LocalIO, _WaitingForKB
    from ultrakb.kb_admin import delete_kb
    from ultrakb.lifecycle import exclusive_lifecycle

    monkeypatch.setattr(
        "ultrakb.config.GLOBAL_CONFIG_PATH", kb_dir.with_name(kb_dir.name + "-global.yaml")
    )

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    io = LocalIO()
    attempts, results, writes = [], [], []
    io.completed.connect(lambda _sequence, _value, error: attempts.append(error))

    def until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate()

    try:
        with exclusive_lifecycle(kb_dir):
            io.submit(
                lambda: writes.append("stale setting"),
                lambda value, error: results.append((value, error)),
                kb=kb_dir,
                exclusive=True,
            )
            until(lambda: any(isinstance(error, _WaitingForKB) for error in attempts))
            delete_kb(kb_dir)
            initialize_kb(kb_dir, seed_environment=False)
        until(lambda: bool(results))
        assert isinstance(results[0][1], FileNotFoundError)
        assert writes == []
    finally:
        io.stop()
        until(io.stopped)


def test_snapshot_io_reads_published_knowledge_while_writer_holds_lease(kb_dir):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ultrakb.application.pages import read_page
    from ultrakb.application.views import published_scope
    from ultrakb.desktop.io import LocalIO
    from ultrakb.desktop.verification_data import publish_pages
    from ultrakb.locks import atomic_write_text, kb_ingest_lock

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    atomic_write_text(kb_dir / "wiki/concepts/first.md", "Published body")
    publish_pages(kb_dir, {"concepts/first.md": b"Published body"})
    io = LocalIO()
    entered, release = threading.Event(), threading.Event()
    result = []

    def writing():
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(writing)
        assert entered.wait(5)
        try:
            io.submit(
                lambda: read_page(kb_dir, "concepts/first", scope=published_scope(kb_dir)),
                lambda value, error: result.append((value, error)),
                kb=kb_dir,
                snapshot=True,
            )
            deadline = time.monotonic() + 2
            while not result and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert result and result[0][1] is None
            assert result[0][0].body == "Published body"
        finally:
            release.set()
            io.stop()
        writer.result(5)


def test_workbench_opens_public_wiki_before_writer_dependent_loads(kb_dir):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from ultrakb.desktop.verification_data import publish_pages, retain_document

    retain_document(kb_dir, "original.md", "original", "Frozen original source")
    publish_pages(kb_dir, {"concepts/first.md": b"Published body"})
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; from pathlib import Path; "
            "runpy.run_path(sys.argv[1])['_workbench_read'](Path(sys.argv[2]))",
            str(Path(__file__).resolve()),
            str(kb_dir),
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _workbench_read(kb):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ultrakb.desktop.window import Workbench
    from ultrakb.locks import atomic_write_text, kb_ingest_lock

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = Workbench(history_dir=kb / "desktop-test-tasks")
    entered, release = threading.Event(), threading.Event()

    def writer():
        with kb_ingest_lock(kb / ".ultrakb"):
            atomic_write_text(kb / "wiki/concepts/first.md", "Unpublished working body")
            entered.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(writer)
        assert entered.wait(5)
        try:
            window.open_knowledge_base(kb)
            deadline = time.monotonic() + 3
            while window.page is None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert window.page is not None
            assert window.page.body == "Published body"
            assert window.page.validity == "current"
            assert window.read_scope.publication_id == window.page.publication_id
            assert window.workspaces.knowledge.entries[0].path == "concepts/first.md"
            window.shell.navigate("资料")
            panel = window.workspaces.panels["资料"][0]
            deadline = time.monotonic() + 3
            while (panel._inventory_loading or not panel.rows) and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert not panel._inventory_loading
            assert len(panel.rows) == 1
            assert next(iter(panel.rows.values())).actions["read"].allowed
            assert panel.read_scope.publication_id == window.page.publication_id
            release.set()
            writing.result(5)
            _check_atomic_refresh(window, app, kb)
        finally:
            release.set()
            writing.result(5)
            window.request_quit()
            deadline = time.monotonic() + 10
            while not window.shutdown_complete() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert window.shutdown_complete()
            window.timer.stop()


def _check_atomic_refresh(window, app, kb):
    import threading

    from ultrakb.application.pages import save_page
    from ultrakb.desktop import reader

    saved = save_page(kb, "concepts/first", "Updated public body")
    assert saved.status == "saved"
    previous_id = window.page.publication_id
    render = reader.render_markdown
    rendering, release = threading.Event(), threading.Event()

    def delayed(source, *args, **kwargs):
        if source == "Updated public body":
            rendering.set()
            assert release.wait(5)
        return render(source, *args, **kwargs)

    reader.render_markdown = delayed
    try:
        window._refresh_current()
        deadline = time.monotonic() + 3
        while not rendering.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert rendering.is_set()
        assert window.page.body == "Published body"
        assert window.read_scope.publication_id == previous_id
        assert "Published body" in window.reader.toPlainText()
        release.set()
        deadline = time.monotonic() + 3
        while window.page.body != "Updated public body" and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.page.body == "Updated public body"
        assert window.page.publication_id != previous_id
        assert window.page.publication_id == window.read_scope.publication_id
        assert "Updated public body" in window.reader.toPlainText()
    finally:
        release.set()
        reader.render_markdown = render


@pytest.mark.parametrize("page_change", ["refresh", "navigation"])
def test_refresh_keeps_session_catalog_independent_of_page_changes(
    kb_dir, monkeypatch, page_change
):
    from types import SimpleNamespace

    from ultrakb.desktop import knowledge_loading

    requests, applied, catalogs = [], [], []
    window = SimpleNamespace(
        kb=kb_dir,
        view_id=None,
        page=None,
        _open_request_id=1,
        _page_request_id=1,
        _knowledge_request_id=0,
        _deleting_kbs=set(),
        _error=lambda error: bool(error),
        io=SimpleNamespace(
            submit=lambda operation, callback, **options: requests.append(
                (operation, callback, options)
            )
        ),
        conversations=SimpleNamespace(catalog_loaded=catalogs.append),
    )

    def apply_refresh(window, knowledge, **options):
        applied.append(knowledge)
        window._page_request_id += 1
        # A worker can check cancellation before apply_knowledge returns.
        assert not requests[1][2]["obsolete"]()

    monkeypatch.setattr(knowledge_loading, "apply_knowledge", apply_refresh)
    knowledge_loading.refresh(window)
    read_knowledge, knowledge_loaded, knowledge_options = requests[0]
    read_sessions, sessions_loaded, session_options = requests[1]
    knowledge = read_knowledge()
    if page_change == "navigation":
        window._page_request_id += 1
    knowledge_loaded(knowledge, None)
    assert applied == ([knowledge] if page_change == "refresh" else [])
    assert knowledge_options["obsolete"]()
    assert not session_options["obsolete"]()
    sessions_loaded(read_sessions(), None)
    assert catalogs == [[]]
    window._open_request_id += 1
    assert session_options["obsolete"]()
    sessions_loaded([], None)
    assert catalogs == [[]]
