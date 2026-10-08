"""Short Windows scratch paths retain live ownership and crash recovery."""

import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def windows_temp(tmp_path, monkeypatch):
    from openkb.office import workspace

    temporary = tmp_path / "temp"
    temporary.mkdir()
    monkeypatch.setattr(workspace, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(workspace.tempfile, "gettempdir", lambda: str(temporary))
    return temporary


def test_windows_workspaces_are_independent_of_storage_depth_and_keep_live_owners(
    tmp_path, windows_temp
):
    from openkb.office.workspace import office_directory

    deep = tmp_path.joinpath(*(["nested-storage"] * 15))
    with office_directory(deep, prefix="office-test-") as first:
        assert first.is_relative_to(windows_temp)
        assert len(str(first)) < len(str(deep))
        (first / "active").write_text("still owned")
        with office_directory(deep, prefix="office-test-") as second:
            assert second != first
            assert (first / "active").read_text() == "still owned"
        assert not second.exists()
        assert first.exists()
    assert not first.parent.exists()


def test_windows_workspace_is_reaped_after_its_owner_crashes(tmp_path, windows_temp):
    from openkb.office.workspace import office_directory

    receipt = tmp_path / "orphan.txt"
    code = (
        "import os,sys; from pathlib import Path; from types import SimpleNamespace; "
        "from openkb.office import workspace; "
        "workspace.sys=SimpleNamespace(platform='win32'); "
        "workspace.tempfile.gettempdir=lambda:sys.argv[1]; "
        "context=workspace.office_directory(None,prefix='office-test-'); "
        "directory=context.__enter__(); "
        "Path(sys.argv[2]).write_text(str(directory)); "
        "folder=directory/'Documents'; folder.mkdir(); "
        "folder.chmod(0o444) if sys.platform=='win32' else None; os._exit(7)"
    )
    result = subprocess.run([sys.executable, "-c", code, str(windows_temp), str(receipt)])
    assert result.returncode == 7
    orphan = Path(receipt.read_text())
    assert orphan.is_dir()
    with office_directory(None, prefix="office-test-"):
        assert not orphan.parent.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows read-only folder attributes")
def test_windows_workspace_cleans_readonly_office_folders(windows_temp):
    from openkb.office.workspace import office_directory

    with office_directory(None, prefix="office-test-") as directory:
        folder = directory / "Documents"
        folder.mkdir()
        child = folder / "profile.ini"
        child.write_text("temporary settings")
        child.chmod(stat.S_IREAD)
        folder.chmod(stat.S_IREAD)
    assert not directory.parent.exists()


def test_posix_workspace_stays_in_the_owned_input_directory(tmp_path, monkeypatch):
    from openkb.office import workspace

    monkeypatch.setattr(workspace, "sys", SimpleNamespace(platform="linux"))
    with workspace.office_directory(tmp_path, prefix="office-test-") as directory:
        assert directory.parent == tmp_path
    assert not directory.exists()
