"""Keep the A03/A04 native control driver covered separately from desktop evidence."""

import json
import os
import subprocess
import sys

import pytest


def test_native_creation_and_settings_inheritance(tmp_path):
    pytest.importorskip("PySide6")
    environment = dict(os.environ, PYTHONPATH=".", QT_QPA_PLATFORM="offscreen")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_config_inheritance.py",
            "--headless",
            "--output",
            str(tmp_path / "verification"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads((tmp_path / "verification/result.json").read_text())
    assert evidence["headless"] and len(evidence["checks"]) == 6
