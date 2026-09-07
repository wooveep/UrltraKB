"""THROWAWAY prototype: prepare local, pinned renderer resources, never the main app."""

import hashlib
import json
import os
import platform
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
NODE_VERSION = "24.20.0"
NODE_FILES = {
    "Linux": (
        "linux-x64.tar.xz",
        "2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2",
    ),
    "Windows": ("win-x64.zip", "6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba"),
}
FONT_HASHES = {
    "Regular": "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b",
    "Bold": "b5f0d1a190a7f9b43c310a8850630af12553df32c4c050543f9059732d9b4c0a",
}


def node_path():
    name = "node.exe" if os.name == "nt" else "bin/node"
    suffix = "win-x64" if os.name == "nt" else "linux-x64"
    return RUNTIME / f"node-v{NODE_VERSION}-{suffix}" / name


def fetch(url, target, expected=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        print(f"Downloading {target.name}", flush=True)
        with urllib.request.urlopen(url, timeout=90) as response:
            target.write_bytes(response.read())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if expected and digest != expected:
        raise RuntimeError(f"Checksum mismatch: {target}")
    return {"url": url, "file": str(target.relative_to(ROOT)), "sha256": digest}


def main():
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("Prototype currently targets x86_64 only")
    suffix, digest = NODE_FILES[platform.system()]
    archive_name = f"node-v{NODE_VERSION}-{suffix}"
    archive = RUNTIME / archive_name
    manifest = [
        fetch(
            f"https://nodejs.org/download/release/v{NODE_VERSION}/{archive_name}", archive, digest
        )
    ]
    if not node_path().exists():
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as stream:
                stream.extractall(RUNTIME)
        else:
            with tarfile.open(archive) as stream:
                stream.extractall(RUNTIME, filter="data")
    base = "https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004"
    for weight in ("Regular", "Bold"):
        name = f"NotoSansCJKsc-{weight}.otf"
        manifest.append(
            fetch(
                f"{base}/Sans/OTF/SimplifiedChinese/{name}",
                RUNTIME / "fonts" / name,
                FONT_HASHES[weight],
            )
        )
    manifest.append(
        fetch(
            f"{base}/LICENSE",
            RUNTIME / "fonts" / "LICENSE",
            "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2",
        )
    )
    npm = node_path().parent / (
        "node_modules/npm/bin/npm-cli.js"
        if os.name == "nt"
        else "../lib/node_modules/npm/bin/npm-cli.js"
    )
    command = "ci" if (ROOT / "package-lock.json").exists() else "install"
    subprocess.run(
        [str(node_path()), str(npm), command, "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=ROOT,
        check=True,
    )
    (ROOT / "resource-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        "Local Node, npm packages and font files ready. No browser runtime requested.", flush=True
    )


if __name__ == "__main__":
    main()
