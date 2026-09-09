"""Verify an OCR bundle and install its complete wheel closure without network access.

Invoke with the bundle's python/bin/python3 (Linux) or python/python.exe
(Windows), using -I -B. Keep the bundle at its final location: a venv refers
to its base interpreter. The destination must be new and outside the bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from pathlib import Path


def install(bundle: Path, destination: Path, seconds: float):
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("An explicit positive installation time bound is required")
    started = time.monotonic()
    bundle, destination = bundle.resolve(), destination.resolve()
    if destination.exists() or destination.is_relative_to(bundle):
        raise ValueError("Choose a new destination outside the immutable bundle")
    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    if (
        manifest["schema"] != 1
        or manifest["platform"] != sys.platform
        or platform.machine().lower() not in {"x86_64", "amd64"}
        or sys.version_info[:3] != (3, 12, 13)
        or Path(sys.base_prefix).resolve() != bundle / "python"
    ):
        raise ValueError("Use this platform's bundled Python 3.12.13 interpreter")

    def remaining():
        value = seconds - (time.monotonic() - started)
        if value <= 0:
            raise TimeoutError("OCR installation time bound exhausted")
        return value

    for relative, expected in manifest["files"].items():
        remaining()
        path = bundle / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Bundle manifest path escapes package")
        if "symlink" in expected:
            if not path.is_symlink() or str(path.readlink()) != expected["symlink"]:
                raise ValueError(f"Bundle symlink mismatch: {relative}")
            if not path.resolve().is_relative_to(bundle):
                raise ValueError("Bundle symlink escapes package")
        else:
            if path.is_symlink() or not path.resolve().is_relative_to(bundle):
                raise ValueError("Bundle file escapes package")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if path.stat().st_size != expected["bytes"] or digest != expected["sha256"]:
                raise ValueError(f"Bundle file digest mismatch: {relative}")
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PIP_NO_INDEX": "1"}

    def run(command):
        subprocess.run(command, check=True, env=environment, timeout=remaining())

    run([sys.executable, "-I", "-B", "-m", "venv", str(destination)])
    executable = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run(
        [
            str(executable),
            "-I",
            "-B",
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-index",
            "--no-deps",
            "--no-cache-dir",
            "--require-hashes",
            "--only-binary=:all:",
            "--find-links",
            str(bundle / "wheelhouse"),
            "-r",
            str(bundle / "requirements.txt"),
        ]
    )
    run(
        [
            str(executable),
            "-I",
            "-B",
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "check",
        ]
    )
    run([str(executable), "-I", "-B", str(bundle / "verify_ocr_runtime.py")])
    receipt = {
        "interpreter": str(executable),
        "assets": str(bundle / "assets"),
        "assets_sha256": manifest["assets_sha256"],
        "python": manifest["python"],
        "platform": sys.platform,
        "seconds": time.monotonic() - started,
        "bundle_sha256": hashlib.sha256((bundle / "bundle.json").read_bytes()).hexdigest(),
    }
    (destination / "openkb-installation.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--seconds", type=float, required=True)
    args = parser.parse_args()
    install(Path(__file__).resolve().parent, args.destination, args.seconds)
