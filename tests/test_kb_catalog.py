"""Desktop discovery is by path, without the REST alias collision filtering."""

import json

import pytest


def test_catalog_includes_root_children_and_same_named_registered_paths(tmp_path, monkeypatch):
    from openkb import config
    from openkb.application.catalog import knowledge_bases

    root = tmp_path / "root"
    child, other, ghost = root / "notes", tmp_path / "elsewhere/notes", tmp_path / "missing"
    for path in (child, other):
        (path / ".openkb").mkdir(parents=True)
        (path / "wiki").mkdir()
    global_path = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", global_path)
    monkeypatch.setenv("OPENKB_KB_ROOT", str(root))
    global_path.write_text(
        json.dumps(
            {
                "known_kbs": [str(child), str(other), str(ghost)],
                "kb_aliases": {"again": str(child)},
            }
        )
    )
    entries = knowledge_bases()
    assert len(entries) == 3
    assert {path for _, path in entries} == {child, other, ghost}


def test_old_unverified_process_summary_does_not_block_directory_management(tmp_path):
    import uuid

    from openkb.runtime.records import TaskView
    from openkb.runtime.tasks import TaskManager

    root, history = tmp_path / "kb", tmp_path / "history"
    history.mkdir()
    view = TaskView(
        uuid.uuid4().hex, str(root), "SavePage", "running", "saving", 1, (), False, False
    )
    (history / f"{view.id}.json").write_text(json.dumps({"view": view.summary(), "identities": []}))
    manager = TaskManager(history_dir=history)
    try:
        assert manager.get(view.id).state == "interrupted"
        assert not manager.has_work(root)
        assert not manager.get(view.id).processes_reaped  # Retain the historical uncertainty.
    finally:
        manager.shutdown(stop=True)
        assert manager.join(5)


def test_catalog_keeps_unfinished_deletion_at_its_original_path(tmp_path, monkeypatch):
    import pytest

    from openkb import config
    from openkb.application.catalog import knowledge_bases
    from openkb.kb_admin import delete_kb
    from openkb.lifecycle import deletion_binding

    original, victim = tmp_path / "a/notes", tmp_path / "b/notes"
    for path in (original, victim):
        (path / ".openkb").mkdir(parents=True)
        (path / "wiki").mkdir()
    global_path = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", global_path)
    monkeypatch.setenv("OPENKB_KB_ROOT", str(tmp_path / "a"))
    global_path.write_text(json.dumps({"known_kbs": [str(original), str(victim)]}))
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise OSError("partial deletion")

        patch.setattr("openkb.kb_admin.shutil.rmtree", fail)
        with pytest.raises(OSError):
            delete_kb(original)
    original.rename(tmp_path / "retained")
    try:
        original.symlink_to(victim, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    assert {path for _, path in knowledge_bases()} == {original, victim}
    with pytest.raises(ValueError, match="symbolic link"):
        deletion_binding(original)
    assert victim.is_dir()


def test_partial_delete_of_unregistered_root_child_remains_discoverable(tmp_path, monkeypatch):
    import shutil

    import pytest

    from openkb import config
    from openkb.application.catalog import knowledge_bases
    from openkb.kb_admin import delete_kb

    root = tmp_path / "kbs/notes"
    (root / ".openkb").mkdir(parents=True)
    (root / "wiki").mkdir()
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global.yaml")
    monkeypatch.setenv("OPENKB_KB_ROOT", str(root.parent))
    config.GLOBAL_CONFIG_PATH.write_text(json.dumps({"default_kb": "keep-default"}))
    rmtree = shutil.rmtree

    def partial(path, *args, **kwargs):
        if path == root:
            rmtree(root / "wiki")
            raise OSError("removed wiki, raw still open")
        return rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("openkb.kb_admin.shutil.rmtree", partial)
        with pytest.raises(OSError):
            delete_kb(root)
    assert ("notes", root) in knowledge_bases()
    assert config.load_global_config()["default_kb"] == "keep-default"
    delete_kb(root)
    assert not root.exists()
    assert config.load_global_config()["default_kb"] == "keep-default"


@pytest.mark.parametrize("entry", ["cli", "rest"])
def test_partial_root_deletion_retry_never_selects_another_same_named_kb(
    tmp_path, monkeypatch, entry
):
    import asyncio
    import shutil

    from openkb import config
    from openkb.kb_admin import delete_kb

    root, other = tmp_path / "kbs/notes", tmp_path / "elsewhere/notes"
    for path in (root, other):
        (path / ".openkb").mkdir(parents=True)
        (path / "wiki").mkdir()
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global.yaml")
    monkeypatch.setenv("OPENKB_KB_ROOT", str(root.parent))
    config.GLOBAL_CONFIG_PATH.write_text(json.dumps({"known_kbs": [str(other)]}))
    rmtree = shutil.rmtree

    def partial(path, *args, **kwargs):
        if path == root:
            rmtree(root / "wiki")
            raise OSError("removed wiki, raw still open")
        return rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("openkb.kb_admin.shutil.rmtree", partial)
        with pytest.raises(OSError):
            delete_kb(root)
    if entry == "cli":
        from click.testing import CliRunner

        from openkb.cli import cli

        result = CliRunner().invoke(cli, ["delete-kb", "notes", "--yes"])
        assert "Unfinished deletion" in result.output
    else:
        from fastapi import HTTPException

        from openkb.api_kbs_router import delete_kb_endpoint
        from openkb.api_models import KbDeleteRequest

        with pytest.raises(HTTPException, match="Unfinished deletion"):
            asyncio.run(delete_kb_endpoint(KbDeleteRequest(kb="notes", confirm_name="notes")))
    assert root.is_dir()
    assert (other / "wiki").is_dir()
    # Explicit path selection in the native catalog can finish the original deletion.
    delete_kb(root)
    assert not root.exists()
    assert (other / "wiki").is_dir()
