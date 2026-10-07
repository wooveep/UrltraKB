"""Stage the official Office app intact, outside PyInstaller's binary rewriting."""

import shutil
import subprocess
from pathlib import Path


def extract(archive: Path, stage: Path, runtime: Path) -> None:
    mount = stage / "mounted"
    mount.mkdir()
    subprocess.run(
        ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(archive)],
        check=True,
        timeout=120,
    )
    try:
        app = mount / "LibreOffice.app"
        if not (app / "Contents/MacOS/soffice").is_file():
            raise ValueError("Official Office DMG does not contain LibreOffice.app")
        shutil.copytree(app, runtime / "LibreOffice.app", symlinks=True)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount)], check=True, timeout=60)


def sign(app: Path) -> None:
    # Adding application fonts changes the resource seal. Keep the upstream
    # nested binaries, entitlements and hardened-runtime flags unchanged.
    subprocess.run(
        [
            "codesign",
            "--force",
            "--sign",
            "-",
            "--preserve-metadata=entitlements,flags,runtime",
            str(app),
        ],
        check=True,
    )
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
