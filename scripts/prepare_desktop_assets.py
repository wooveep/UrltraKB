"""Assemble pinned browser-free runtimes for a source or portable desktop build."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "openkb/rendering"
NODE_VERSION = "24.20.0"
NODE_HASHES = {
    "linux-x64.tar.xz": "2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2",
    "win-x64.zip": "6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba",
}
FONTS = {
    "NotoSansCJKsc-Regular.otf": "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b",
    "NotoSansCJKsc-Bold.otf": "b5f0d1a190a7f9b43c310a8850630af12553df32c4c050543f9059732d9b4c0a",
    "LICENSE": "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2",
}
logger = logging.getLogger(__name__)


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def download(url: str, target: Path, expected: str) -> None:
    if target.exists() and digest(target) == expected:
        return
    temporary = target.with_suffix(target.suffix + ".download")
    logger.info("Fetching pinned resource: %s", target.name)
    with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    if digest(temporary) != expected:
        temporary.unlink()
        raise ValueError(f"Checksum mismatch: {target.name}")
    temporary.replace(target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "build/desktop-downloads")
    args = parser.parse_args()
    cache = args.cache_dir.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    suffix = "win-x64.zip" if os.name == "nt" else "linux-x64.tar.xz"
    filename = f"node-v{NODE_VERSION}-{suffix}"
    archive = cache / filename
    download(
        f"https://nodejs.org/download/release/v{NODE_VERSION}/{filename}",
        archive,
        NODE_HASHES[suffix],
    )
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(cache)
        node_root = cache / f"node-v{NODE_VERSION}-win-x64"
        node = node_root / "node.exe"
        npm = node_root / "node_modules/npm/bin/npm-cli.js"
    else:
        with tarfile.open(archive) as bundle:
            bundle.extractall(cache, filter="data")
        node_root = cache / f"node-v{NODE_VERSION}-linux-x64"
        node = node_root / "bin/node"
        npm = node_root / "lib/node_modules/npm/bin/npm-cli.js"
    assets = SOURCE / "assets"
    assets.mkdir(exist_ok=True)
    shutil.copy2(node, assets / node.name)
    shutil.copy2(node_root / "LICENSE", assets / "NODE-LICENSE")
    for name in ("package.json", "package-lock.json", "mathjax_render.mjs"):
        shutil.copy2(SOURCE / name, assets / name)
    subprocess.run(
        [str(node), str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=assets,
        check=True,
    )
    fonts = assets / "fonts"
    fonts.mkdir(exist_ok=True)
    base = "https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/"
    for name, checksum in FONTS.items():
        url = base + (name if name == "LICENSE" else "Sans/OTF/SimplifiedChinese/" + name)
        download(url, fonts / name, checksum)
    subprocess.run(
        ["cargo", "build", "--release", "--locked"], cwd=SOURCE / "rust-helper", check=True
    )
    extension = ".exe" if os.name == "nt" else ""
    shutil.copy2(
        SOURCE / "rust-helper/target/release" / f"openkb-render-prototype{extension}",
        assets / f"renderer{extension}",
    )
    manifest = {
        str(path.relative_to(assets)): digest(path)
        for path in sorted(assets.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    (assets / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info(
        "Assembled %s files. Distribution licence/source assembly is a separate build step.",
        len(manifest),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
