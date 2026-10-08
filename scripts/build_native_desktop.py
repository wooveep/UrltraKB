"""Freeze the desktop with the native document runtimes supported by this host."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from desktop_platform import desktop_target
from prepare_desktop_assets import download
from prepare_office_runtime import verify

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--office-only", action="store_true", help="Verify Office without freezing")
    args = parser.parse_args()
    target = desktop_target()

    def run(script, *arguments):
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), *map(str, arguments)],
            cwd=ROOT,
            check=True,
        )

    if not args.office_only and target.system in {"Linux", "Windows"}:
        run("prepare_cfb_helper.py")
    if target.name not in {"debian-amd64", "windows-x64", "macos-arm64"}:
        if args.office_only:
            parser.error("This native target has no pinned Office runtime")
        # Debian arm64 has no matching pinned upstream Office distribution.
        run("build_desktop.py")
        return

    lock = json.loads((ROOT / "openkb/office/runtime-lock.json").read_text("utf-8"))
    cache = args.cache_dir.resolve() / "office"
    cache.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for key in (sys.platform, "source"):
        record = lock[key]
        path = cache / Path(urlsplit(record["url"]).path).name
        download(record["url"], path, record["sha256"])
        verify(path, record)
        artifacts.append(path)
    run(
        "build_office_desktop.py",
        "--office-archive",
        artifacts[0],
        "--office-source",
        artifacts[1],
        *(["--verify-only"] if args.office_only else []),
    )


if __name__ == "__main__":
    main()
