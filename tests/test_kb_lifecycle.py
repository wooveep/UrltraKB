"""Deleting a directory must exclude old and new work through final cleanup."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from openkb.kb_admin import delete_kb
from openkb.locks import kb_ingest_lock, session_lock


@pytest.fixture(autouse=True)
def isolated_global_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.config.GLOBAL_CONFIG_PATH", tmp_path.with_name(tmp_path.name + "-global.yaml")
    )


def test_delete_keeps_new_writers_out_until_directory_is_gone(kb_dir, monkeypatch):
    import openkb.kb_admin as admin

    deleting, finish, waiting, entered = (threading.Event() for _ in range(4))
    original = admin.shutil.rmtree

    def paused_delete(path, *args, **kwargs):
        if Path(path) == kb_dir:
            deleting.set()
            assert finish.wait(5)
        return original(path, *args, **kwargs)

    def write():
        with kb_ingest_lock(kb_dir / ".openkb", on_wait=waiting.set):
            entered.set()
            (kb_dir / "wiki/late.md").write_text("must not be written")

    monkeypatch.setattr(admin.shutil, "rmtree", paused_delete)
    with ThreadPoolExecutor(max_workers=2) as pool:
        removal = pool.submit(delete_kb, kb_dir)
        assert deleting.wait(5)
        writer = pool.submit(write)
        try:
            assert waiting.wait(2), "Writer entered while rmtree was still running"
            assert not entered.is_set()
        finally:
            finish.set()
        removal.result(5)
        with pytest.raises(FileNotFoundError):
            writer.result(5)
    assert not kb_dir.exists()


def test_delete_waits_for_session_owner_even_before_its_kb_lease(kb_dir, monkeypatch):
    import openkb.kb_admin as admin

    removed, session_ready, finish = (threading.Event() for _ in range(3))
    original = admin.shutil.rmtree

    def observe(path, *args, **kwargs):
        if Path(path) == kb_dir:
            removed.set()
        return original(path, *args, **kwargs)

    def conversation():
        with session_lock(kb_dir, "pending"):
            session_ready.set()
            assert finish.wait(5)
            with kb_ingest_lock(kb_dir / ".openkb"):
                assert kb_dir.is_dir()

    monkeypatch.setattr(admin.shutil, "rmtree", observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(conversation)
        assert session_ready.wait(5)
        removal = pool.submit(delete_kb, kb_dir)
        try:
            assert not removed.wait(0.2), "Deletion raced an active session lock"
        finally:
            finish.set()
        owner.result(5)
        removal.result(5)
    assert not kb_dir.exists()


def test_partial_delete_blocks_normal_access_and_can_finish_same_directory(kb_dir, monkeypatch):
    import openkb.kb_admin as admin
    from openkb.mutation import RecoveryRequired

    original = admin.shutil.rmtree
    (kb_dir / "wiki/index.md").write_text("partially removed")

    def fail(path, *args, **kwargs):
        if Path(path) == kb_dir:
            (kb_dir / "wiki/index.md").unlink()
            raise OSError("partial deletion")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(admin.shutil, "rmtree", fail)
    with pytest.raises(OSError):
        delete_kb(kb_dir)
    with pytest.raises(RecoveryRequired, match="deletion is incomplete"):
        with kb_ingest_lock(kb_dir / ".openkb"):
            pytest.fail("Partially deleted KB admitted ordinary work")
    monkeypatch.setattr(admin.shutil, "rmtree", original)
    delete_kb(kb_dir)
    assert not kb_dir.exists()


def test_partial_delete_never_resumes_against_a_replaced_directory(kb_dir, monkeypatch):
    import openkb.kb_admin as admin

    original = admin.shutil.rmtree

    def fail(path, *args, **kwargs):
        if Path(path) == kb_dir:
            raise OSError("partial deletion")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(admin.shutil, "rmtree", fail)
    with pytest.raises(OSError):
        delete_kb(kb_dir)
    kb_dir.rename(kb_dir.with_name(kb_dir.name + "-old"))
    kb_dir.mkdir()
    sentinel = kb_dir / "unrelated.txt"
    sentinel.write_text("keep")
    monkeypatch.setattr(admin.shutil, "rmtree", original)
    with pytest.raises(ValueError, match="replaced directory"):
        delete_kb(kb_dir)
    assert sentinel.read_text() == "keep"


def test_queued_native_request_cannot_write_into_recreated_kb(kb_dir, tmp_path):
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.application.pages import read_page
    from openkb.lifecycle import exclusive_lifecycle
    from openkb.runtime.requests import SavePage
    from openkb.runtime.tasks import TaskManager

    target = kb_dir / "wiki/concepts/note.md"
    target.write_text("# Original\n")
    original = read_page(kb_dir, "concepts/note")
    manager = TaskManager(history_dir=kb_dir.with_name(kb_dir.name + "-history"))
    try:
        # No child can get its KB lease before we remove and recreate it.
        with exclusive_lifecycle(kb_dir):
            task = manager.submit(
                kb_dir, [SavePage(original.path, "old queued edit", original.version)]
            )
            delete_kb(kb_dir)
            initialize_kb(kb_dir, seed_environment=False)
            target.write_text("# Original\n")  # Same page CAS; directory identity must still win.
        result = manager.wait(task, timeout=20)
        assert result.state == "blocked"
        assert result.started_at is None
        assert result.processes_reaped
        assert target.read_text() == "# Original\n"
    finally:
        manager.shutdown(stop=True)
        assert manager.join(20)


def test_old_execution_cannot_follow_a_replaced_root(kb_dir):
    import shutil

    from openkb.lifecycle import current_generation, expected_generation

    (kb_dir / "wiki/index.md").write_text("keep")
    generation = current_generation(kb_dir)
    retained = kb_dir.with_name(kb_dir.name + "-retained")
    kb_dir.rename(retained)
    shutil.copytree(retained, kb_dir)
    with pytest.raises(FileNotFoundError, match="removed or recreated"):
        with expected_generation(kb_dir, generation), kb_ingest_lock(kb_dir / ".openkb"):
            pytest.fail("Old execution entered the replacement KB")
    # A new caller can explicitly open the current copy at this path.
    with kb_ingest_lock(kb_dir / ".openkb"):
        assert (kb_dir / "wiki/index.md").is_file()


def test_partial_delete_refuses_a_symlink_replacement(kb_dir, monkeypatch):
    import shutil

    import openkb.kb_admin as admin

    (kb_dir / "wiki/index.md").write_text("keep")
    victim = kb_dir.with_name(kb_dir.name + "-victim")
    shutil.copytree(kb_dir, victim)
    original = admin.shutil.rmtree

    def fail(path, *args, **kwargs):
        if Path(path) == kb_dir:
            raise OSError("partial deletion")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(admin.shutil, "rmtree", fail)
    with pytest.raises(OSError):
        delete_kb(kb_dir)
    kb_dir.rename(kb_dir.with_name(kb_dir.name + "-retained"))
    try:
        kb_dir.symlink_to(victim, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable for this Windows account")
    monkeypatch.setattr(admin.shutil, "rmtree", original)
    with pytest.raises(ValueError, match="replaced directory|symbolic link"):
        delete_kb(kb_dir)
    assert (victim / "wiki/index.md").is_file()


@pytest.mark.parametrize("entry", ["cli", "rest"])
@pytest.mark.parametrize("name", ["old", " old "])
def test_deletion_alias_cannot_hide_a_replaced_directory(kb_dir, monkeypatch, entry, name):
    import asyncio
    import json
    import shutil

    from openkb import config

    victim = kb_dir.with_name(kb_dir.name + "-victim")
    shutil.copytree(kb_dir, victim)
    kb_dir.rename(kb_dir.with_name(kb_dir.name + "-retained"))
    try:
        kb_dir.symlink_to(victim, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    config.GLOBAL_CONFIG_PATH.write_text(json.dumps({"kb_aliases": {"old": str(kb_dir)}}))
    if entry == "cli":
        from click.testing import CliRunner

        from openkb.cli import cli

        result = CliRunner().invoke(cli, ["delete-kb", name, "--yes"])
        assert "symbolic link" in result.output
    else:
        from fastapi import HTTPException

        from openkb.api_kbs_router import delete_kb_endpoint
        from openkb.api_models import KbDeleteRequest

        with pytest.raises(HTTPException) as error:
            asyncio.run(delete_kb_endpoint(KbDeleteRequest(kb=name, confirm_name=name)))
        assert error.value.status_code == 400
    assert (victim / ".openkb/config.yaml").is_file()


def test_cli_confirmation_does_not_authorize_a_replacement(kb_dir, monkeypatch):
    import json
    import shutil

    from click.testing import CliRunner

    from openkb import config
    from openkb.cli import cli

    config.GLOBAL_CONFIG_PATH.write_text(json.dumps({"kb_aliases": {"old": str(kb_dir)}}))

    def confirm(*args, **kwargs):
        retained = kb_dir.with_name(kb_dir.name + "-retained")
        kb_dir.rename(retained)
        shutil.copytree(retained, kb_dir)
        return "old"

    monkeypatch.setattr("click.prompt", confirm)
    result = CliRunner().invoke(cli, ["delete-kb", "old"])
    assert result.exit_code != 0
    assert "replaced directory" in result.output
    assert (kb_dir / ".openkb/config.yaml").is_file()


def test_deletion_alias_preserves_parent_segments_after_a_symlink(tmp_path, monkeypatch):
    from openkb.config import resolve_kb_alias
    from openkb.kb_admin import resolve_deletion_alias

    child = tmp_path / "remote/child"
    child.mkdir(parents=True)
    shortcut = tmp_path / "shortcut"
    try:
        shortcut.symlink_to(child, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    for root in (tmp_path / "kbs/demo", tmp_path / "remote/kbs/demo"):
        (root / ".openkb").mkdir(parents=True)
        (root / "wiki").mkdir()
    monkeypatch.setenv("OPENKB_KB_ROOT", str(shortcut / "../kbs"))
    assert resolve_deletion_alias("demo") == resolve_kb_alias("demo")
    # Windows normalizes parent segments differently; both must identify the
    # directory the operating system opens through this original path.
    assert resolve_deletion_alias("demo").samefile(shortcut / "../kbs/demo")
