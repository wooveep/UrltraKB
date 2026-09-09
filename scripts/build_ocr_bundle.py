"""Build the optional, platform-specific OCR bundle from frozen, hashed inputs.

Run with the locked OCR runtime's Python on each target OS. This is the explicit
network acquisition step; install_ocr_runtime.py performs no dependency resolving.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import shutil
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
import tomllib

from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def selected_wheels(config: Path) -> list[dict]:
    lock = tomllib.loads((config / "uv.lock").read_text(encoding="utf-8"))
    tags = {tag: rank for rank, tag in enumerate(sys_tags())}
    selected = []
    for line in (config / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        matches = [
            package
            for package in lock["package"]
            if package["name"] == canonicalize_name(requirement.name)
            and package["version"] in requirement.specifier
        ]
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one locked version for {requirement.name}")
        package = matches[0]
        candidates = []
        for wheel in package.get("wheels", []):
            name = unquote(Path(urlsplit(wheel["url"]).path).name)
            _, _, _, compatible = parse_wheel_filename(name)
            ranks = [tags[tag] for tag in compatible if tag in tags]
            if ranks:
                candidates.append((min(ranks), name, wheel))
        if not candidates:
            raise ValueError(f"No locked binary wheel for {requirement.name} on this host")
        _, filename, wheel = min(candidates, key=lambda entry: (entry[0], entry[1]))
        selected.append(
            {"name": package["name"], "version": package["version"], "file": filename, **wheel}
        )
    return sorted(selected, key=lambda wheel: wheel["name"])


def build(output: Path, assets: Path, *, seconds: float, download_bytes: int, disk_bytes: int):
    if sys.version_info[:3] != (3, 12, 13) or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("Build on Windows/Linux x64 using the locked Python 3.12.13 runtime")
    if (
        sys.platform not in {"linux", "win32"}
        or not math.isfinite(seconds)
        or min(seconds, download_bytes, disk_bytes) <= 0
    ):
        raise ValueError("Unsupported platform or nonpositive build bounds")
    output = output.resolve()
    if output.exists():
        raise ValueError("Use a new output directory; incomplete builds are never silently reused")
    started = time.monotonic()
    config = ROOT / "packaging/ocr-runtime"
    wheels = selected_wheels(config)
    interpreter = json.loads((config / "interpreters.json").read_text())[sys.platform]
    declared = sum(wheel["size"] for wheel in wheels) + interpreter["size"]
    if declared > download_bytes:
        raise ValueError(f"Locked downloads need {declared} bytes, exceeding this build's bound")
    output.mkdir(parents=True)
    acquired = 0

    def checkpoint():
        if time.monotonic() - started >= seconds:
            raise TimeoutError("OCR bundle build time exhausted")

    def download(url: str, target: Path, expected: str, size: int):
        nonlocal acquired
        checkpoint()
        target.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=(10, 30)) as response:
            response.raise_for_status()
            received = 0
            with target.open("xb") as stream:
                for chunk in response.iter_content(128 * 1024):
                    checkpoint()
                    received += len(chunk)
                    acquired += len(chunk)
                    if received > size or acquired > download_bytes:
                        raise ValueError("OCR bundle download byte bound exhausted")
                    stream.write(chunk)
        if target.stat().st_size != size or digest(target) != expected:
            raise ValueError(f"Downloaded digest/size mismatch: {target.name}")

    archive = output / "python.tar.gz"
    download(interpreter["url"], archive, interpreter["sha256"], interpreter["size"])
    with tarfile.open(archive) as package:
        expanded = sum(member.size for member in package.getmembers())
        if expanded + declared > disk_bytes:
            raise ValueError("Interpreter exceeds build disk bound")
        package.extractall(output, filter="data")
    requirements = []
    for wheel in wheels:
        target = output / "wheelhouse" / wheel["file"]
        download(wheel["url"], target, wheel["hash"].removeprefix("sha256:"), wheel["size"])
        requirements.append(f"{wheel['name']}=={wheel['version']} --hash={wheel['hash']}")
        with zipfile.ZipFile(target) as package:
            for name in package.namelist():
                path = Path(name)
                if name.endswith("/") or path.is_absolute() or ".." in path.parts:
                    continue
                if any(word in name.lower() for word in ("license", "copying", "notice")) or (
                    name.endswith(".dist-info/METADATA")
                ):
                    destination = output / "licenses" / wheel["name"] / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(package.read(name))
        print(f"Acquired {wheel['name']}=={wheel['version']}", flush=True)

    assets_manifest = json.loads((assets / "manifest.json").read_text(encoding="utf-8"))
    for relative, info in assets_manifest["files"].items():
        checkpoint()
        path = Path(relative)
        source = assets / path
        if (
            path.is_absolute()
            or ".." in path.parts
            or not source.resolve().is_relative_to(assets.resolve())
        ):
            raise ValueError("Model asset path escapes package")
        if source.stat().st_size != info["bytes"] or digest(source) != info["sha256"]:
            raise ValueError("Model asset digest mismatch")
        expanded += info["bytes"]
        if expanded + declared > disk_bytes:
            raise ValueError("Model assets exceed build disk bound")
        target = output / "assets" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(assets / "manifest.json", output / "assets/manifest.json")
    (output / "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
    for name in ("pyproject.toml", "uv.lock", "requirements.lock", "interpreters.json"):
        target = output / "build-inputs" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(config / name, target)
    for name in (
        "install_ocr_runtime.py",
        "verify_ocr_runtime.py",
        "build_ocr_bundle.py",
        "build_ocr_assets.py",
    ):
        shutil.copyfile(ROOT / "scripts" / name, output / name)

    files = {}
    total = 0
    for path in sorted(output.rglob("*")):
        checkpoint()
        relative = path.relative_to(output).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(output):
                raise ValueError("Interpreter symlink escapes bundle")
            files[relative] = {"symlink": str(path.readlink())}
        elif path.is_file():
            size = path.stat().st_size
            total += size
            if total > disk_bytes:
                raise ValueError("OCR bundle disk bound exhausted")
            files[relative] = {"sha256": digest(path), "bytes": size}
    manifest = {
        "schema": 1,
        "platform": sys.platform,
        "machine": "x86_64",
        "python": "3.12.13",
        "interpreter": interpreter,
        "wheels": wheels,
        "files": files,
        "assets_sha256": digest(output / "assets/manifest.json"),
    }
    (output / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"download_bytes": acquired, "disk_bytes": total, "seconds": time.monotonic() - started}
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--download-bytes", type=int, required=True)
    parser.add_argument("--disk-bytes", type=int, required=True)
    args = parser.parse_args()
    build(
        args.output,
        args.assets,
        seconds=args.seconds,
        download_bytes=args.download_bytes,
        disk_bytes=args.disk_bytes,
    )
