"""Build the desktop, then stage the complete Office sidecar before inventory.

The sidecar is deliberately outside PyInstaller's ABI/dependency processing.
It is inside _internal so all existing archive copying and path checks apply.
"""

import argparse
import json
import subprocess
import sys
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
    build_desktop()
    prepare(
        args.office_archive,
        args.office_source,
        root / "packaging/desktop/dist/UrltraKB/_internal/office",
        launcher,
    )


if __name__ == "__main__":
    main()
