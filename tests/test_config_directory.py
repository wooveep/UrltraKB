"""Private application configuration survives actual worker process boundaries."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_a_new_process_uses_its_explicit_application_configuration_directory(tmp_path):
    private = tmp_path / "isolated-config"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ultrakb.config import GLOBAL_CONFIG_DIR; print(GLOBAL_CONFIG_DIR)",
        ],
        env={**os.environ, "ULTRAKB_CONFIG_DIR": str(private)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == str(private)


def test_actual_process_registration_leaves_user_configuration_unchanged(tmp_path):
    private = tmp_path / "worker-config"
    actual = Path.home() / ".config/ultrakb/global.yaml"
    before = actual.read_bytes() if actual.exists() else None
    root = tmp_path / "registered-kb"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from ultrakb.config import register_kb; register_kb(Path(sys.argv[1]))",
            str(root),
        ],
        env={**os.environ, "ULTRAKB_CONFIG_DIR": str(private)},
        capture_output=True,
        text=True,
        check=True,
    )
    import yaml

    saved = yaml.safe_load((private / "global.yaml").read_text())
    assert saved["known_kbs"] == [str(root)]
    assert saved["default_kb"] == str(root)
    assert (actual.read_bytes() if actual.exists() else None) == before
