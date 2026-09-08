"""Freeze the checked-out application using the provisioned, locked environment."""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from export_desktop_source import verify_source

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging/desktop"


def main() -> None:
    identity = verify_source(ROOT)
    if version("openkb") != identity["version"]:
        raise ValueError("Install this exported source with its recorded version before freezing")
    cache = PACKAGING / "build/token-cache"
    cache.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, TIKTOKEN_CACHE_DIR=str(cache))
    # Tiktoken verifies the downloaded vocabulary against its pinned hashes.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import tiktoken; "
            "[tiktoken.get_encoding(e) for e in "
            "('cl100k_base', 'o200k_base', 'p50k_base', 'r50k_base')]",
        ],
        env=environment,
        check=True,
    )

    environment.update(LITELLM_LOCAL_MODEL_COST_MAP="True", OTEL_SDK_DISABLED="true")
    if os.name == "nt":
        # Windows 11 supplies API sets/UCRT. Do not discover downlevel shim
        # DLLs from unrelated SDKs or developer utilities on the launch PATH.
        windows = Path(os.environ["SystemRoot"])
        environment["PATH"] = os.pathsep.join(
            map(
                str,
                (Path(sys.executable).parent, Path(sys.base_prefix), windows / "System32", windows),
            )
        )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(PACKAGING / "dist"),
            "--workpath",
            str(PACKAGING / "build/freeze"),
            str(PACKAGING / "desktop.spec"),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    if verify_source(ROOT) != identity:
        raise ValueError("Source identity changed during the build")


if __name__ == "__main__":
    main()
