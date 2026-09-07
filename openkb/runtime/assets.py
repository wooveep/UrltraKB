"""Locate frozen SDK resources without initializing the desktop or SDKs."""

import os
import sys
from pathlib import Path


def configure_sdk_resources() -> None:
    """Called only in CLI/API processes or an isolated execution child."""
    if getattr(sys, "frozen", False):
        cache = Path(__file__).resolve().parents[1] / "token-cache"
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(cache))
