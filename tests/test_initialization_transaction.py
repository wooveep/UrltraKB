"""Creation is one recoverable KB mutation, including inherited content."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from openkb.application.knowledge_bases import initialize_kb


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    from openkb import config

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")


def test_failed_creation_restores_existing_files_and_can_be_retried(tmp_path, monkeypatch):
    from openkb.application import knowledge_bases

    root = tmp_path / "new"
    (root / "wiki").mkdir(parents=True)
    (root / "wiki/index.md").write_text("existing notes")
    (root / "raw").mkdir()
    (root / "raw/keep.md").write_text("owned input")
    original = knowledge_bases.save_config

    def fail_after_config(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("injected storage failure")

    monkeypatch.setattr(knowledge_bases, "save_config", fail_after_config)
    with pytest.raises(OSError):
        initialize_kb(root)
    assert (root / "wiki/index.md").read_text() == "existing notes"
    assert (root / "raw/keep.md").read_text() == "owned input"
    assert not (root / ".openkb/config.yaml").exists()
    monkeypatch.setattr(knowledge_bases, "save_config", original)
    assert initialize_kb(root)["created"]


@pytest.mark.parametrize("entry", ["initialize", "open", "repair"])
@pytest.mark.parametrize("point", ["config", "intent"])
def test_creation_crash_recovers_before_explicit_retry(tmp_path, entry, point):
    root = tmp_path / "new"
    code = """
import os, sys
from pathlib import Path
from openkb.application import knowledge_bases as kb
if sys.argv[2] == 'config':
    original = kb.save_config
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(23)
    kb.save_config = crash
else:
    original = Path.unlink
    def crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if self.name == 'initializing.json':
            os._exit(23)
    Path.unlink = crash
kb.initialize_kb(Path(sys.argv[1]), seed_environment=False)
"""
    result = subprocess.run([sys.executable, "-c", code, str(root), point], timeout=20)
    assert result.returncode == 23
    assert list((root / ".openkb/journal").glob("*.json"))
    if entry == "open":
        from openkb.application.knowledge_bases import open_kb

        with pytest.raises(ValueError, match="not initialized"):
            open_kb(root)
    elif entry == "repair":
        from openkb.application.repair import repair_knowledge_base

        repaired = repair_knowledge_base(root)
        assert repaired.repaired and not repaired.initialized
    from openkb.application.knowledge_bases import initialization_pending

    if entry != "initialize":
        assert initialization_pending(root)
    assert initialize_kb(root, seed_environment=False, require_empty=True)["created"]
    assert not initialization_pending(root)
    assert not list((root / ".openkb/journal").glob("*.json"))


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
    assert not (root / ".openkb/config.yaml").exists()
