"""Native dialog acceptance is isolated from user settings and live model work."""

import json
import os
import subprocess
import sys

import pytest


def test_failed_summary_retry_native_confirmation(tmp_path):
    pytest.importorskip("PySide6")
    output = tmp_path / "native"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_summary_recovery.py",
            "--headless",
            "--output",
            str(output),
        ],
        env=dict(os.environ, PYTHONPATH=".", PYTHON_DOTENV_DISABLED="1"),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / "result.json").read_text())
    assert len(report["checks"]) == 4 and report["model_sends"] == 2
