"""An inventory refresh may supersede a read, but must retain a current click."""

from types import SimpleNamespace

import pytest


@pytest.mark.native
@pytest.mark.parametrize("refresh,switch", [(False, False), (True, False), (True, True)])
def test_current_question_intent_survives_inventory_refresh(tmp_path, refresh, switch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ultrakb.desktop.import_progress import ImportProgress
    from ultrakb.desktop.verification import create_application

    app = QApplication.instance() or create_application()
    queued, opened = [], []

    class IO:
        def submit(self, read, loaded, **kwargs):
            queued.append((loaded, kwargs["obsolete"]))

    window = SimpleNamespace(io=IO(), _open_request_id=0, _quitting=False)
    progress = ImportProgress(window)
    progress.root, progress.view_id, progress._terminal = tmp_path, "legacy", "completed"
    progress.open_target = lambda *args: opened.append(args)
    rows = [SimpleNamespace(actions={"ask": SimpleNamespace(allowed=True)})]
    try:
        progress.begin_question()
        if switch:
            window._open_request_id += 1
        if refresh:
            progress.refresh_available(tmp_path)
        for callback, obsolete in queued:
            if not obsolete():
                callback(rows, None)
        assert opened == ([] if switch else [(tmp_path, "legacy")])
    finally:
        progress.timer.stop()
        progress.deleteLater()
        app.processEvents()


@pytest.mark.native
def test_read_started_before_click_cannot_consume_that_click(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ultrakb.desktop.import_progress import ImportProgress
    from ultrakb.desktop.verification import create_application

    app = QApplication.instance() or create_application()
    queued, opened = [], []

    class IO:
        def submit(self, read, loaded, **kwargs):
            queued.append(loaded)

    window = SimpleNamespace(io=IO(), _open_request_id=0, _quitting=False)
    progress = ImportProgress(window)
    progress.root, progress.view_id = tmp_path, "legacy"
    progress.open_target = lambda *args: opened.append(args)
    rows = [SimpleNamespace(actions={"ask": SimpleNamespace(allowed=True)})]
    try:
        progress.check_available()
        progress.begin_question()
        queued[0](rows, None)
        assert opened == []
        queued[1](rows, None)
        assert opened == [(tmp_path, "legacy")]
    finally:
        progress.timer.stop()
        progress.deleteLater()
        app.processEvents()
