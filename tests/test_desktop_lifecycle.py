"""Qt event loops plus real spawned workers exercise shutdown and a fresh restart."""

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("PySide6")


@pytest.mark.parametrize("mode", ["wait", "stop", "delete"])
def test_shutdown_remains_observable_and_restart_never_replays(tmp_path, mode):
    initial, restarted = tmp_path / mode, tmp_path / "restarted"

    def run(output, *arguments):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "openkb.desktop.verification",
                "--output",
                str(output),
                "--lifecycle",
                *arguments,
            ],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads((output / "lifecycle.json").read_text("utf-8"))

    first = run(initial, mode)
    assert first["shutdown_progress_and_diagnostics_visible"]
    assert first["owned_work_reaped"]
    second = run(restarted, "restart", "--lifecycle-state", str(initial))
    assert second["history_retained_without_replay_or_watch"]
