"""Catch settings geometry and wheel regressions through the real native workbench."""

import os
import subprocess
import sys

import pytest

pytest.importorskip("PySide6")


@pytest.mark.parametrize("scale", ["1", "1.5"])
def test_native_settings_and_artifact_layout(tmp_path, scale):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "openkb.desktop.verification",
            "--settings-layout",
            "--output",
            str(tmp_path / "settings-layout"),
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
