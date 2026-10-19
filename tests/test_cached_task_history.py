"""An unavailable knowledge base cannot hide a confirmed cached worker result."""

import pytest

from ultrakb.application.pages import read_page
from ultrakb.desktop.verification_data import publish_pages
from ultrakb.runtime.requests import SavePage
from ultrakb.runtime.tasks import TaskManager
from ultrakb.storage.database import database_path


@pytest.mark.parametrize("unavailable", ["missing", "unsupported"])
def test_cached_task_survives_an_unavailable_main_store(kb_dir, tmp_path_factory, unavailable):
    history = tmp_path_factory.mktemp("cached-task-history")
    publish_pages(kb_dir, {"concepts/note.md": b"Original."})
    manager = TaskManager(history_dir=history, max_workers=1)
    try:
        task_id = manager.submit(
            kb_dir,
            [SavePage("concepts/note", "Confirmed.", read_page(kb_dir, "concepts/note").version)],
        )
        saved = manager.wait(task_id, timeout=30)
        assert saved.state == "completed" and saved.succeeded == 1
    finally:
        manager.shutdown(stop=True)
        assert manager.join(30)
    master = database_path(kb_dir)
    original = master.read_bytes()
    offline = kb_dir.with_name(kb_dir.name + "-offline")
    if unavailable == "missing":
        kb_dir.rename(offline)
    else:
        master.write_bytes(b"Unsupported main store")
    restarted = None
    try:
        restarted = TaskManager(history_dir=history, max_workers=1)
        cached = restarted.get(task_id)
        assert cached.state == "completed" and cached.succeeded == 1
        assert cached.results[0].resources == saved.results[0].resources
        assert cached.results[0].revision == saved.results[0].revision
        assert "cached" in cached.error.lower() and "unavailable" in cached.error.lower()
        assert len(restarted.tasks()) == 1
        assert restarted.wait(task_id, timeout=0).succeeded == 1
    finally:
        if restarted is not None:
            restarted.shutdown(stop=True)
            assert restarted.join(30)
        if unavailable == "missing":
            offline.rename(kb_dir)
        else:
            master.write_bytes(original)
    assert read_page(kb_dir, "concepts/note").body == "Confirmed."
