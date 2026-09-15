"""Exercise settings navigation under the same write lease as a running task."""

import os
import subprocess
import sys

import pytest

pytest.importorskip("PySide6")


def test_settings_load_while_document_task_is_running(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "openkb.desktop.verification",
            "--settings-busy",
            "--output",
            str(tmp_path / "settings-busy"),
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
