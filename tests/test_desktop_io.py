"""Native local I/O keeps its original KB identity across deferred attempts."""

import time

import pytest

QtCore = pytest.importorskip("PySide6.QtCore")


def test_local_io_retry_does_not_rebind_to_a_recreated_kb(kb_dir, monkeypatch):
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.desktop.io import LocalIO, _WaitingForKB
    from openkb.kb_admin import delete_kb
    from openkb.lifecycle import exclusive_lifecycle

    monkeypatch.setattr(
        "openkb.config.GLOBAL_CONFIG_PATH", kb_dir.with_name(kb_dir.name + "-global.yaml")
    )

    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
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
