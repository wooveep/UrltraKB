"""Desktop discovery is by path, without the REST alias collision filtering."""

import json

import pytest


def test_catalog_includes_root_children_and_same_named_registered_paths(tmp_path, monkeypatch):
    from ultrakb import config
    from ultrakb.application.catalog import knowledge_bases

    root = tmp_path / "root"
    child, other, ghost = root / "notes", tmp_path / "elsewhere/notes", tmp_path / "missing"
    for path in (child, other):
        (path / ".ultrakb").mkdir(parents=True)
        from kb_fixtures import mark_current_kb

        mark_current_kb(path)
        (path / ".ultrakb/config.yaml").write_text("{}\n")
        (path / "wiki").mkdir()
    global_path = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", global_path)
    monkeypatch.setenv("ULTRAKB_KB_ROOT", str(root))
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

    from ultrakb.runtime.records import TaskView
    from ultrakb.runtime.tasks import TaskManager

    root, history = tmp_path / "kb", tmp_path / "history"
    from ultrakb.application.knowledge_bases import initialize_kb

    initialize_kb(root, seed_environment=False)
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

    from ultrakb import config
    from ultrakb.application.catalog import knowledge_bases
    from ultrakb.kb_admin import delete_kb
    from ultrakb.lifecycle import deletion_binding

    original, victim = tmp_path / "a/notes", tmp_path / "b/notes"
    for path in (original, victim):
        (path / ".ultrakb").mkdir(parents=True)
        from kb_fixtures import mark_current_kb

        mark_current_kb(path)
        (path / ".ultrakb/config.yaml").write_text("{}\n")
        (path / "wiki").mkdir()
    global_path = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", global_path)
    monkeypatch.setenv("ULTRAKB_KB_ROOT", str(tmp_path / "a"))
    global_path.write_text(json.dumps({"known_kbs": [str(original), str(victim)]}))
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise OSError("partial deletion")

        patch.setattr("ultrakb.kb_admin.shutil.rmtree", fail)
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

    from ultrakb import config
    from ultrakb.application.catalog import knowledge_bases
    from ultrakb.kb_admin import delete_kb

    root = tmp_path / "kbs/notes"
    (root / ".ultrakb").mkdir(parents=True)
    from kb_fixtures import mark_current_kb

    mark_current_kb(root)
    (root / ".ultrakb/config.yaml").write_text("{}\n")
    (root / "wiki").mkdir()
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global.yaml")
    monkeypatch.setenv("ULTRAKB_KB_ROOT", str(root.parent))
    config.GLOBAL_CONFIG_PATH.write_text(json.dumps({"default_kb": "keep-default"}))
    rmtree = shutil.rmtree

    def partial(path, *args, **kwargs):
        if path == root:
            rmtree(root / "wiki")
            raise OSError("removed wiki, raw still open")
        return rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("ultrakb.kb_admin.shutil.rmtree", partial)
        with pytest.raises(OSError):
            delete_kb(root)
    assert ("notes", root) in knowledge_bases()
    assert config.load_global_config()["default_kb"] == "keep-default"
    delete_kb(root)
    assert not root.exists()
    assert config.load_global_config()["default_kb"] == "keep-default"


@pytest.mark.parametrize("entry", ["cli", "rest"])
def test_partial_native_deletion_retry_keeps_the_original_root_identity(
    tmp_path, monkeypatch, entry
):
    import asyncio
    import shutil

    from ultrakb import config
    from ultrakb.kb_admin import delete_kb

    root, other = tmp_path / "kbs/notes", tmp_path / "elsewhere/notes"
    for path in (root, other):
        (path / ".ultrakb").mkdir(parents=True)
        from kb_fixtures import mark_current_kb

        mark_current_kb(path)
        (path / ".ultrakb/config.yaml").write_text("{}\n")
        (path / "wiki").mkdir()
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global.yaml")
    monkeypatch.setenv("ULTRAKB_KB_ROOT", str(root.parent))
    config.GLOBAL_CONFIG_PATH.write_text(json.dumps({"known_kbs": [str(other)]}))
    rmtree = shutil.rmtree

    def partial(path, *args, **kwargs):
        if path == root:
            rmtree(root / "wiki")
            raise OSError("removed wiki, raw still open")
        return rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("ultrakb.kb_admin.shutil.rmtree", partial)
        with pytest.raises(OSError):
            delete_kb(root)
    if entry == "cli":
        from click.testing import CliRunner

        from ultrakb.cli import cli

        result = CliRunner().invoke(cli, ["delete-kb", "notes", "--yes"])
        assert result.exit_code == 0, result.output
    else:
        from ultrakb.api_kbs_router import delete_kb_endpoint
        from ultrakb.api_models import KbDeleteRequest

        asyncio.run(delete_kb_endpoint(KbDeleteRequest(kb="notes", confirm_name="notes")))
    assert not root.exists()
    assert (other / "wiki").is_dir()
