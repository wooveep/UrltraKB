"""Real Qt and storage exercise captured scopes, diagnostic tails and stop scheduling."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_pending_panel_and_native_stop_remain_responsive_during_reclamation(tmp_path):
    script = tmp_path / "pending_desktop.py"
    script.write_text("""import os, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_storage_questions import question_knowledge
from test_storage_discovery import embedded_book
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
from ultrakb.application.pending import process_pending
from ultrakb.application.views import storage_scope
import ultrakb.application.views as views
from ultrakb.knowledge_scope import live_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.discovery_store import DiscoveryStore
from ultrakb.storage.pending_store import PendingStore
from ultrakb.storage.coordination import try_reclamation
from ultrakb.desktop.io import LocalIO
from ultrakb.desktop.pending import PendingDialog
from ultrakb.desktop.window import Workbench
from ultrakb.runtime.tasks import TaskManager
from ultrakb.runtime.requests import RunPendingJob

if __name__ == "__main__":
    app = QApplication([])
    base = Path(sys.argv[1])
    root, identity, _ = question_knowledge(base)
    scope = storage_scope(root, identity)
    store, pending = DiscoveryStore(root, identity), PendingStore(root, identity)
    for index, count in enumerate((66, 1)):
        host = base / f"private{index}.xlsx"
        embedded_book(host, objects=0)
        with ZipFile(host, "a") as package:
            for number in range(count):
                package.writestr(f"xl/embeddings/Private{number:03}.bin", b"private")
        revision = CatalogStore(root, identity).admit(host, source_key=str(host))
        store.schedule(revision, root_import_id=uuid4().hex, view_id=scope.view_id)
        assert process_pending(root, scope=scope)["outcomes"][0]["status"] == "completed"

    class Window(QWidget):
        _schedule_task_control = Workbench._schedule_task_control
        request_task_stop = Workbench.request_task_stop
        def _error(self, error):
            assert error is None, error

    window = Window()
    window.scope, window.read_scope = live_scope(root), scope
    window.io, window.control_io = LocalIO(window), LocalIO(window)
    window.manager = TaskManager(history_dir=base / "history")
    views.view_scope = lambda *args: scope
    panel = PendingDialog(window, root)
    window.read_scope = None
    def until(predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            app.processEvents(); time.sleep(.01)
        assert predicate(), panel.status.text()

    try:
        until(lambda: window.io.stopped() and not panel._busy and panel._detail is not None)
        assert panel.scope is scope and panel.table.rowCount() == 2
        assert len(panel._detail["checkpoints"]) == 63 and panel.more_objects.isEnabled()
        panel.load_more_objects()
        until(lambda: window.io.stopped() and not panel._busy)
        assert len(panel._detail["checkpoints"]) == 66 and not panel.more_objects.isEnabled()
        panel.table.selectRow(1)
        until(lambda: window.io.stopped() and not panel._busy)
        assert len(panel._detail["checkpoints"]) == 1

        work = store.schedule(revision, root_import_id=uuid4().hex, view_id=scope.view_id,
                              processing_key="native-stop")
        claimed = pending.claim(intent_id=work.intent_id)
        with window.manager._condition:
            task = window.manager.submit(root, [RunPendingJob(work.intent_id, claimed.dispatch_id,
                                                            view_id=scope.view_id)])
            with try_reclamation(root, identity) as db:
                assert db is not None
                window.request_task_stop(task)
                assert window.manager.get(task).stop_requested
                pulses = []
                QTimer.singleShot(0, lambda: pulses.append(True))
                until(lambda: bool(pulses))
                assert not window.control_io.stopped()
        until(window.control_io.stopped)
        assert pending.job(work.intent_id).status == "stopped"
        assert window.manager.wait(task, timeout=5).state == "stopped"

        group_id = panel._detail["root_import_id"]
        before = store.group(group_id).budget
        panel.fields["max_sources"].setText("99")
        with try_reclamation(root, identity) as db:
            assert db is not None
            panel.budget()
            until(lambda: not window.io.stopped())
            panel.done(0)
            window.io.stop()
            until(window.io.stopped)
        assert store.group(group_id).budget == before
        assert not (root / ".ultrakb/catalog").exists()
    finally:
        panel.done(0)
        window.io.stop()
        window.control_io.stop()
        window.manager.shutdown(stop=True)
        assert window.manager.join(10)
        until(lambda: window.io.stopped() and window.control_io.stopped())
    print("pending desktop verified")
""")
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path)], capture_output=True, text=True, timeout=45
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-3000:]
    assert "pending desktop verified" in result.stdout
