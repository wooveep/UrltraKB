"""Verify all four native build receipts before collecting GitHub Release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

TARGETS = {
    "debian-amd64": ".deb",
    "debian-arm64": ".deb",
    "windows-x64": ".zip",
    "macos-arm64": ".zip",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def collect(artifacts: Path, output: Path, tag: str, commit: str) -> None:
    if not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag):
        raise ValueError("A stable vMAJOR.MINOR.PATCH tag is required")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("A complete source commit is required")
    selected = []
    seen = set()
    for path in sorted(artifacts.rglob("*-build.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        target = receipt.get("target")
        if target not in TARGETS or target in seen:
            raise ValueError("Unexpected or duplicate native build target")
        if receipt.get("version") != tag[1:] or receipt.get("commit") != commit:
            raise ValueError("Build identity does not match the release tag and commit")
        stem = f"UrltraKB-{tag[1:]}-{target}"
        if path.name != stem + "-build.json":
            raise ValueError("Unexpected build receipt name")
        for suffix, key in [
            (TARGETS[target], "installer_sha256"),
            ("-source.zip", "source_sha256"),
        ]:
            asset = path.with_name(stem + suffix)
            if asset.is_symlink() or not asset.is_file() or digest(asset) != receipt.get(key):
                raise ValueError(f"Release asset missing or checksum mismatch: {asset.name}")
            selected.append(asset)
        selected.append(path)
        seen.add(target)
    if seen != set(TARGETS):
        raise ValueError("All four native build targets are required before publication")
    output.mkdir(parents=True, exist_ok=False)
    checksums = []
    for asset in sorted(selected, key=lambda item: item.name):
        destination = output / asset.name
        shutil.copyfile(asset, destination)
        checksums.append(f"{digest(destination)}  {destination.name}\n")
    (output / "SHA256SUMS.txt").write_text("".join(checksums), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    collect(args.artifacts, args.output, args.tag, args.commit)


if __name__ == "__main__":
    main()
