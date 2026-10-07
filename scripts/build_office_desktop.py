"""Build the desktop, then stage the complete Office sidecar before inventory.

The sidecar is deliberately outside PyInstaller's ABI/dependency processing.
It is inside _internal so all existing archive copying and path checks apply.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from build_desktop import main as build_desktop
from prepare_office_runtime import prepare, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--office-archive", required=True, type=Path)
    parser.add_argument("--office-source", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    lock = json.loads((root / "openkb/office/runtime-lock.json").read_text())
    verify(args.office_archive, lock[sys.platform])
    verify(args.office_source, lock["source"])
    launcher = None
    if sys.platform == "win32":
        project = root / "openkb/office/launcher"
        subprocess.run(["cargo", "build", "--locked", "--release"], cwd=project, check=True)
        launcher = project / "target/release/openkb-office-launcher.exe"
    staging = root / "packaging/desktop/build"
    staging.mkdir(parents=True, exist_ok=True)
    # Verify native Office dependencies before the expensive application freeze.
    # PyInstaller owns dist/UrltraKB, so stage the sidecar outside it until then.
    with tempfile.TemporaryDirectory(prefix="office-native-", dir=staging) as directory:
        office = Path(directory) / "office"
        prepare(args.office_archive, args.office_source, office, launcher)
        if sys.platform == "darwin":
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_office_macos.py",
                ],
                cwd=root,
                env={**os.environ, "OPENKB_TEST_OFFICE_RUNTIME": str(office)},
                check=True,
            )
        build_desktop()
        shutil.move(office, root / "packaging/desktop/dist/UrltraKB/_internal/office")


if __name__ == "__main__":
    main()
