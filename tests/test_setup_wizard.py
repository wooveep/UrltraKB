"""Verify first-use setup and post-setup chat at the actual Qt call site."""

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("scale", ["1", "1.25"])
def test_native_setup_and_chat_dispatch(tmp_path, scale):
    pytest.importorskip("PySide6")
    output = tmp_path / "verification"
    result = subprocess.run(
        [sys.executable, "scripts/verify_setup_wizard.py", "--headless", "--output", str(output)],
        env=dict(os.environ, PYTHONPATH=".", QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / "result.json").read_text())
    assert report["scale"] == scale and len(report["checks"]) == 8
    assert report["verification_mode"] == "retained-history"
    assert report["model_probe_requests"] == 1
    assert "HTTP 401" in report["model_probe_result"]
