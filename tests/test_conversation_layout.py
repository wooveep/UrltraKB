"""Verify resized conversation behavior without model execution."""

import json
import os
import subprocess
import sys

import pytest


def test_fluid_conversation_preserves_input_and_scroll(tmp_path):
    pytest.importorskip("PySide6")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_conversation_layout.py",
            "--headless",
            "--output",
            str(tmp_path / "gui"),
        ],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH="."),
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "gui/result.json").read_text())
    assert len(report["checks"]) == 3 and report["model_sends"] == 0
