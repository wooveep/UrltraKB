"""Interaction/geometry regressions run in isolated native application processes."""

import os
import subprocess
import sys

import pytest

pytest.importorskip("PySide6")


@pytest.mark.native
@pytest.mark.parametrize("scenario", ["navigation", "documents", "progress", "tasks"])
def test_gui_delivery(scenario, tmp_path):
    result = subprocess.run(
        [sys.executable, "tests/gui_delivery_probe.py", scenario, "--output", str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": os.getcwd()},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
