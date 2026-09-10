"""Run workbench acceptance in an isolated Qt process."""

import os
import subprocess
import sys

import pytest

pytest.importorskip("PySide6")


@pytest.mark.parametrize("scale", ["1", "1.5"])
def test_populated_native_tables_and_document_actions(tmp_path, scale):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "openkb.desktop.verification",
            "--tables",
            "--output",
            str(tmp_path / "tables"),
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_workbench_navigation_and_appearance(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "openkb.desktop.verification",
            "--workbench",
            "--output",
            str(tmp_path / "workbench"),
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    restart = subprocess.run(
        [
            sys.executable,
            "-m",
            "openkb.desktop.verification",
            "--output",
            str(tmp_path / "restart"),
            "--workbench-restart",
            str(tmp_path / "workbench"),
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert restart.returncode == 0, restart.stdout + restart.stderr
