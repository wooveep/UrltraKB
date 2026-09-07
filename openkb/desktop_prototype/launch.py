"""One-command developer setup and launch for the throwaway prototype (not a portable release)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
helper = (
    ROOT
    / "rust-helper"
    / "target"
    / "debug"
    / ("openkb-render-prototype.exe" if os.name == "nt" else "openkb-render-prototype")
)
if not python.exists() or not helper.exists() or "--prepare" in sys.argv:
    if not shutil.which("uv") or not shutil.which("cargo"):
        raise SystemExit(
            "Developer prototype prerequisites: uv and Rust/Cargo 1.95.0 "
            "(Windows: MSVC build tools). See README.md."
        )
    if not python.exists():
        subprocess.run(["uv", "venv", str(ROOT / ".venv"), "--python", "3.12.13"], check=True)
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "-r", str(ROOT / "requirements.txt")],
        check=True,
    )
    subprocess.run([str(python), str(ROOT / "bootstrap.py")], check=True)
    subprocess.run(
        ["cargo", "build", "--locked", "--manifest-path", str(ROOT / "rust-helper" / "Cargo.toml")],
        check=True,
    )
arguments = [arg for arg in sys.argv[1:] if arg != "--prepare"]
raise SystemExit(subprocess.call([str(python), str(ROOT / "run.py"), *arguments], cwd=ROOT))
