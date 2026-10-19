"""Creation is one recoverable KB mutation, including inherited content."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from ultrakb.application.knowledge_bases import initialize_kb


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    from ultrakb import config

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")


def test_failed_creation_restores_existing_files_and_can_be_retried(tmp_path, monkeypatch):
    from ultrakb.application import storage_creation

    root = tmp_path / "new"
    (root / "raw").mkdir(parents=True)
    (root / "raw/keep.md").write_text("owned input")
    original = storage_creation.save_config

    def fail_after_config(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("injected storage failure")

    monkeypatch.setattr(storage_creation, "save_config", fail_after_config)
    with pytest.raises(OSError):
        initialize_kb(root)
    assert not (root / "wiki").exists()
    assert (root / "raw/keep.md").read_text() == "owned input"
    assert not (root / ".ultrakb/config.yaml").exists()
    monkeypatch.setattr(storage_creation, "save_config", original)
    assert initialize_kb(root)["created"]


@pytest.mark.parametrize("entry", ["initialize", "open", "repair"])
@pytest.mark.parametrize("point", ["before_install", "after_install"])
def test_creation_crash_keeps_an_absent_or_complete_native_store(tmp_path, entry, point):
    root = tmp_path / "new"
    code = """
import os, sys
from pathlib import Path
from ultrakb.application import storage_creation as kb
original = os.replace
root = Path(sys.argv[1])
def install(source, target):
    if Path(target) == root / '.ultrakb':
        if sys.argv[2] == 'before_install':
            os._exit(23)
        original(source, target)
        os._exit(23)
    return original(source, target)
kb.os.replace = install
kb.initialize_kb(root, seed_environment=False)
"""
    result = subprocess.run([sys.executable, "-c", code, str(root), point], timeout=20)
    assert result.returncode == 23
    installed = point == "after_install"
    assert (root / ".ultrakb/kb.sqlite").exists() == installed
    if entry == "open":
        from ultrakb.application.knowledge_bases import open_kb

        if installed:
            assert open_kb(root) == root
        else:
            with pytest.raises(ValueError):
                open_kb(root)
    elif entry == "repair":
        from ultrakb.application.repair import repair_knowledge_base

        if installed:
            repaired = repair_knowledge_base(root)
            assert repaired.initialized and repaired.repaired
        else:
            with pytest.raises(ValueError):
                repair_knowledge_base(root)
    if installed:
        from ultrakb.application.views import published_scope

        assert published_scope(root).knowledge.read("index.md")
        with pytest.raises(FileExistsError):
            initialize_kb(root, seed_environment=False)
    else:
        assert initialize_kb(root, seed_environment=False, require_empty=True)["created"]
    assert not (root / ".ultrakb/format.json").exists()


def test_concurrent_creation_has_one_owner(tmp_path):
    root = tmp_path / "new"

    def create(model):
        try:
            return initialize_kb(root, model=model, seed_environment=False)["created"]
        except FileExistsError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, ["openai/one", "openai/two"])) == [False, True]


def test_native_creation_checks_empty_directory_after_recovery(tmp_path):
    root = tmp_path / "new"
    root.mkdir()
    note = root / "unrelated.txt"
    note.write_text("Keep this")
    with pytest.raises(ValueError, match="空目录"):
        initialize_kb(root, require_empty=True)
    assert note.read_text() == "Keep this"
    assert not (root / ".ultrakb/config.yaml").exists()
