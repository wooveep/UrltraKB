"""Probe-only bundle contract; output is always outside the program directory."""

import os
import sys
from pathlib import Path


def assets():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "probe_assets"
    return Path(__file__).resolve().parent / ".runtime"


def helper_environment():
    env = dict(os.environ)
    # These helpers were built independently; do not inject PyInstaller's ELF search path.
    if os.name != "nt":
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        env.pop("LD_LIBRARY_PATH", None)
        if original:
            env["LD_LIBRARY_PATH"] = original
    env["TZ"] = "UTC"
    return env
