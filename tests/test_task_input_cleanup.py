"""Real process crashes must release private inputs without touching user files."""

import json
import multiprocessing as mp
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest


def _crash_with_fixed_input(*args):
    from openkb.runtime.worker import run_unit

    def crash(**kwargs):
        prepared = Path(args[-1])
        copies = [
            path
            for path in prepared.rglob("*")
            if path.is_file()
            and any(part.startswith("openkb-input-") for part in path.parts)
            and path.suffix in {".md", ".png"}
        ]
        (Path(args[1].kb_dir) / "crash-evidence.json").write_text(
            json.dumps([str(path) for path in copies]), encoding="utf-8"
        )
        os._exit(86)

    with patch("litellm.completion", side_effect=crash):
        run_unit(*args)


def _doomed_url_parent(kb, history, evidence):
    from openkb.locks import kb_ingest_lock
    from openkb.runtime.input_store import child_preparation
    from openkb.runtime.requests import ImportUrl
    from openkb.runtime.tasks import TaskManager

    manager = TaskManager(history_dir=history, max_workers=1)
    with kb_ingest_lock(kb / ".openkb"):
        task = manager.submit(kb, [ImportUrl("https://example.invalid/article")])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with manager._condition:
                if manager.get(task).state == "waiting" and not manager._active:
                    break
            time.sleep(0.01)
        else:
            raise TimeoutError("Worker did not release its waiting lease")
        directory = Path(manager._preparations.name) / "orphaned-url" / "unit"
        directory.mkdir(parents=True)
        with child_preparation(directory):
            cached = directory / "article.md"
            cached.write_text("Privately acquired URL input", encoding="utf-8")
            evidence.write_text(json.dumps({"copy": str(cached), "task": task}), encoding="utf-8")
            os._exit(87)


def test_restart_discards_orphaned_url_preparation_and_preserves_live_instance(kb_dir, tmp_path):
    from openkb.runtime.tasks import TaskManager

    history = tmp_path / "history"
    unrelated = history / ".inputs" / ("f" * 32) / "data/keep.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("No application ownership record", encoding="utf-8")
    live = TaskManager(history_dir=history)
    live_input = Path(live._preparations.name) / "active-copy.md"
    live_input.write_text("Still owned by a live application", encoding="utf-8")
    evidence = tmp_path / "parent-evidence.json"
    process = mp.get_context("spawn").Process(
        target=_doomed_url_parent, args=(kb_dir, history, evidence)
    )
    process.start()
    process.join(30)
    try:
        assert not process.is_alive()
        assert process.exitcode == 87
        saved = json.loads(evidence.read_text("utf-8"))
        assert Path(saved["copy"]).exists()
        restarted = TaskManager(history_dir=history)
        try:
            assert restarted.get(saved["task"]).state == "interrupted"
            assert not restarted.has_work(kb_dir)
            assert not Path(saved["copy"]).exists()
            assert live_input.read_text("utf-8") == "Still owned by a live application"
            assert unrelated.read_text("utf-8") == "No application ownership record"
        finally:
            restarted.shutdown(stop=True)
            assert restarted.join(10)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()
        live.shutdown(stop=True)
        assert live.join(10)


def test_worker_crash_reclaims_fixed_document_and_images(kb_dir, tmp_path):
    import openkb.runtime.tasks as tasks
    from openkb.runtime.requests import ImportFile

    source = tmp_path / "source.md"
    image = tmp_path / "figure.png"
    source.write_text("# Input\n![figure](figure.png)\n", encoding="utf-8")
    from PIL import Image

    Image.new("RGB", (80, 80), "blue").save(image)
    image_bytes = image.read_bytes()
    with patch.object(tasks, "run_unit", _crash_with_fixed_input):
        manager = tasks.TaskManager(history_dir=tmp_path / "history")
        try:
            task = manager.submit(kb_dir, [ImportFile(str(source))])
            result = manager.wait(task, timeout=30)
            assert result.state == "interrupted" and result.processes_reaped
            copies = json.loads((kb_dir / "crash-evidence.json").read_text("utf-8"))
            assert len(copies) == 2
            assert not any(Path(p).exists() for p in copies)
            assert source.read_text("utf-8") == "# Input\n![figure](figure.png)\n"
            assert image.read_bytes() == image_bytes
        finally:
            manager.shutdown(stop=True)
            assert manager.join(10)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limit")
def test_completed_units_do_not_exhaust_cleanup_handles(tmp_path):
    script = """
import resource
import sys
from pathlib import Path
from openkb.runtime.input_store import InputStore, child_preparation

store = InputStore(Path(sys.argv[1]))
for index in range(80):
    directory = Path(store.name) / "task" / str(index)
    directory.mkdir(parents=True)
    with child_preparation(directory):
        (directory / "private.md").write_text("Private input", encoding="utf-8")
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(64, soft), hard))
store.cleanup()
assert not store.group.exists()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "history")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def _holding_worker(*args):
    from openkb.runtime.worker import run_unit

    def hold(**kwargs):
        from types import SimpleNamespace

        from tests.http_model_fixture import evidence_response

        frozen = next(Path(args[-1]).rglob("original.md"))
        root = Path(args[1].kb_dir).parent
        (root / "worker-ready.json").write_text(
            json.dumps({"copy": str(frozen), "group": str(args[-1].parents[2])}),
            encoding="utf-8",
        )
        deadline = time.monotonic() + 30
        while not (root / "release-worker").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Worker release did not arrive")
            time.sleep(0.01)
        (root / "worker-read.txt").write_text(frozen.read_text("utf-8"), encoding="utf-8")
        payload = json.loads(kwargs["messages"][-1]["content"])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(evidence_response(payload)))
                )
            ],
            usage=None,
        )

    with patch("litellm.completion", side_effect=hold):
        run_unit(*args)


def _doomed_active_parent(kb, history, source):
    import openkb.runtime.tasks as tasks
    from openkb.runtime.requests import ImportFile

    with patch.object(tasks, "run_unit", _holding_worker):
        manager = tasks.TaskManager(history_dir=history, max_workers=1)
        task = manager.submit(kb, [ImportFile(str(source))])
        deadline = time.monotonic() + 20
        while not (kb.parent / "worker-ready.json").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Worker did not start")
            time.sleep(0.01)
        with manager._condition:
            assert task in manager._active
            (kb.parent / "task-id").write_text(task, encoding="utf-8")
            os._exit(88)


def test_active_worker_retains_input_after_parent_crash(kb_dir, tmp_path):
    from openkb.runtime.tasks import TaskManager

    source = tmp_path / "original.md"
    source.write_text("# Input still needed by an active worker\n", encoding="utf-8")
    history = tmp_path / "history"
    process = mp.get_context("spawn").Process(
        target=_doomed_active_parent, args=(kb_dir, history, source)
    )
    process.start()
    process.join(30)
    try:
        assert not process.is_alive()
        assert process.exitcode == 88
        saved = json.loads((kb_dir.parent / "worker-ready.json").read_text("utf-8"))
        manager = TaskManager(history_dir=history)
        try:
            task = (kb_dir.parent / "task-id").read_text("utf-8")
            assert manager.get(task).state == "interrupted"
            assert not manager.has_work(kb_dir)
            if sys.platform == "win32":
                # The parent-owned Job Object kills its workers on close.
                # Restart can reclaim their inputs immediately after that exit.
                assert not Path(saved["copy"]).exists()
                assert not Path(saved["group"]).exists()
                assert not (kb_dir.parent / "worker-read.txt").exists()
                return
            assert Path(saved["copy"]).read_text("utf-8") == source.read_text("utf-8")
            (kb_dir.parent / "release-worker").touch()
            deadline = time.monotonic() + 15
            while Path(saved["group"]).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (kb_dir.parent / "worker-read.txt").read_text("utf-8") == source.read_text(
                "utf-8"
            )
            assert not Path(saved["group"]).exists()
        finally:
            manager.shutdown(stop=True)
            assert manager.join(10)
    finally:
        (kb_dir.parent / "release-worker").touch()
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()
