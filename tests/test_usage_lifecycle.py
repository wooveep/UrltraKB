"""Usage receipts belong to the same directory lifecycle as the import."""

import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_lifecycle(tmp_path, monkeypatch):
    from openkb import config, lifecycle

    profile = tmp_path.with_name(tmp_path.name + "-profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", profile)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", profile / "global.yaml")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_LOCK_PATH", profile / ".lock")
    sidecars = tmp_path.with_name(tmp_path.name + "-lifecycle")

    def paths(root):
        identity = hashlib.sha256(str(root).encode()).hexdigest()
        return sidecars / (identity + ".lock"), sidecars / (identity + ".json")

    monkeypatch.setattr(lifecycle, "_paths", paths)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_deletion_after_usage_precheck_does_not_recreate_kb(kb_dir, monkeypatch, asynchronous):
    from openkb.application.documents import import_document
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.application.recompilation import recompile_document
    from openkb.kb_admin import delete_kb

    config = kb_dir / ".openkb/config.yaml"
    original = Path.is_file
    deleted = False

    def precheck(path):
        nonlocal deleted
        present = original(path)
        if path == config and present and not deleted:
            deleted = True
            delete_kb(kb_dir)
        return present

    monkeypatch.setattr(Path, "is_file", precheck)
    with pytest.raises((FileNotFoundError, ValueError)):
        if asynchronous:
            asyncio.run(recompile_document(kb_dir, "a" * 32))
        else:
            import_document(kb_dir, kb_dir.with_name("outside.txt"))
    assert deleted
    assert not kb_dir.exists(), "Usage ledger recreated the deleted knowledge base"
    initialize_kb(kb_dir, seed_environment=False)
    assert config.is_file()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("checkpoint", ["started", "completed"])
def test_deletion_waits_for_first_and_final_usage_write(
    kb_dir, monkeypatch, asynchronous, checkpoint
):
    import openkb.kb_admin as admin
    import openkb.llm_usage_execution as usage

    writing, finish, removing, attempted = (threading.Event() for _ in range(4))
    original_write = usage._write_execution
    original_remove = admin.shutil.rmtree

    def write(path, record, **updates):
        if updates.get("state", record.state) == checkpoint:
            writing.set()
            assert finish.wait(5)
        return original_write(path, record, **updates)

    def remove(path, *args, **kwargs):
        if Path(path) == kb_dir:
            removing.set()
        return original_remove(path, *args, **kwargs)

    @usage.track_import_usage
    def operation(kb_dir):
        return {"status": "completed"}

    @usage.track_import_usage
    async def async_operation(kb_dir):
        await asyncio.sleep(0)
        return {"status": "completed"}

    def invoke():
        return asyncio.run(async_operation(kb_dir)) if asynchronous else operation(kb_dir)

    def delete():
        attempted.set()
        admin.delete_kb(kb_dir)

    monkeypatch.setattr(usage, "_write_execution", write)
    monkeypatch.setattr(admin.shutil, "rmtree", remove)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(invoke)
        assert writing.wait(5)
        removal = pool.submit(delete)
        try:
            assert attempted.wait(5)
            assert not removing.wait(0.2), "Deletion passed an active usage write"
        finally:
            finish.set()
        assert work.result(5)["status"] == "completed"
        removal.result(5)
    assert not kb_dir.exists()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_cancelled_usage_boundary_creates_no_execution(kb_dir, asynchronous):
    from openkb.application.execution import ExecutionContext
    from openkb.llm_usage_execution import track_import_usage
    from openkb.locks import LockCancelled

    @track_import_usage
    def operation(kb_dir, *, context):
        pytest.fail("Cancelled operation entered business work")

    @track_import_usage
    async def async_operation(kb_dir, *, context):
        pytest.fail("Cancelled async operation entered business work")

    context = ExecutionContext(cancelled=lambda: True)
    with pytest.raises(LockCancelled):
        if asynchronous:
            asyncio.run(async_operation(kb_dir, context=context))
        else:
            operation(kb_dir, context=context)
    assert not (kb_dir / ".openkb/usage").exists()


def test_async_usage_wait_releases_event_loop(kb_dir):
    from openkb.application.execution import ExecutionContext
    from openkb.lifecycle import exclusive_lifecycle
    from openkb.llm_usage_execution import track_import_usage

    held, finish = threading.Event(), threading.Event()

    def owner():
        with exclusive_lifecycle(kb_dir):
            held.set()
            assert finish.wait(5), "Async usage wait blocked the event loop"

    @track_import_usage
    async def operation(kb_dir, *, context):
        return {"status": "completed"}

    async def invoke():
        waiting = asyncio.Event()
        context = ExecutionContext(on_event=lambda event: waiting.set())
        task = asyncio.create_task(operation(kb_dir, context=context))
        await asyncio.wait_for(waiting.wait(), 5)
        finish.set()
        return await task

    with ThreadPoolExecutor(max_workers=1) as pool:
        lease = pool.submit(owner)
        assert held.wait(5)
        try:
            assert asyncio.run(invoke())["status"] == "completed"
        finally:
            finish.set()
        lease.result(5)
