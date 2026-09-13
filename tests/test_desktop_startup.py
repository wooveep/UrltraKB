"""First paint and repeated launches through real, isolated Qt processes."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")


@pytest.mark.parametrize("mode", ["system", "light", "dark"])
def test_first_frame_uses_bundled_fonts_without_manual_theme_change(tmp_path, mode):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "openkb.desktop.verification",
            "--startup",
            mode,
            "--output",
            str(tmp_path / mode),
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# Use the production bootstrap, replacing only the expensive business window.
# Every launch has the same user profile even when its working directory differs.
_LAUNCH = r"""
import os, sys, types
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
from openkb.desktop import instance
root = Path(sys.argv.pop(1))
instance.instance_directory = lambda: root
class Workbench(QWidget):
    def __init__(self):
        super().__init__()
        with (root / 'owners').open('a') as stream:
            stream.write(str(os.getpid()) + '\n')
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(20)
        QTimer.singleShot(50, self.hide)
    def _show_window(self):
        self.showNormal()
        with (root / 'activations').open('a') as stream:
            stream.write(str(os.getpid()) + '\n')
    def poll(self):
        if (root / 'quit').exists():
            QApplication.instance().quit()
module = types.ModuleType('openkb.desktop.window')
module.Workbench = Workbench
sys.modules[module.__name__] = module
from openkb.desktop.bootstrap import main
raise SystemExit(main())
"""


def _wait(condition, seconds=15):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "Desktop process condition timed out"
        time.sleep(0.02)


def _lines(path):
    return path.read_text().splitlines() if path.exists() else []


def _kill(process):
    if sys.platform == "win32":
        # Windows venv launchers own a second Python process. Reap both.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10
        )
    else:
        process.kill()


@pytest.mark.parametrize("crash", [False, True])
def test_simultaneous_launches_activate_one_owner_and_allow_restart(tmp_path, crash):
    processes = []
    environment = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }

    def launch(profile):
        process = subprocess.Popen(
            [sys.executable, "-c", _LAUNCH, str(profile)],
            cwd=tmp_path,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        return process

    profile = tmp_path / "user"
    try:
        launches = [launch(profile) for _ in range(5)]
        _wait(lambda: sum(p.poll() is not None for p in launches) == 4)
        owner = next(p for p in launches if p.poll() is None)
        owner_ids = _lines(profile / "owners")
        assert len(owner_ids) == 1
        # Requests arriving before the window binds are coalesced into one show.
        _wait(lambda: bool(_lines(profile / "activations")))
        assert set(_lines(profile / "activations")) == set(owner_ids)
        for process in launches:
            if process is not owner:
                output = process.communicate(timeout=1)
                assert process.returncode == 0, output
        # Launch again after the existing window has hidden to the tray.
        before = len(_lines(profile / "activations"))
        repeated = launch(profile)
        output = repeated.communicate(timeout=15)
        assert repeated.returncode == 0, output
        assert _lines(profile / "owners") == owner_ids
        assert len(_lines(profile / "activations")) > before
        # Independent user profiles must not block one another.
        other = launch(tmp_path / "other-user")
        _wait(lambda: bool(_lines(tmp_path / "other-user/owners")))
        assert other.poll() is None
        if crash:
            _kill(owner)
        else:
            (profile / "quit").touch()
        owner.communicate(timeout=10)
        (profile / "quit").unlink(missing_ok=True)
        restarted = launch(profile)
        _wait(lambda: len(_lines(profile / "owners")) == 2)
        assert restarted.poll() is None
    finally:
        for process in processes:
            if process.poll() is None:
                _kill(process)
            process.communicate(timeout=10)
