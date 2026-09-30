"""Build and attest the locked native CFB helper before desktop packaging."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "openkb/cfb_helper/rust-helper"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_archive(output, material):
    """Ship an offline rebuildable source tree, including the entire locked closure."""
    with tempfile.TemporaryDirectory(prefix="openkb-cfb-vendor-") as temporary:
        vendor = Path(temporary) / "vendor"
        subprocess.run(
            ["cargo", "vendor", "--locked", "--versioned-dirs", str(vendor)],
            cwd=SOURCE,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in material:
                archive.write(SOURCE / name, name)
            for path in sorted(vendor.rglob("*")):
                if path.is_file():
                    archive.write(path, "vendor/" + path.relative_to(vendor).as_posix())
            archive.writestr(
                ".cargo/config.toml",
                (
                    '[source.crates-io]\nreplace-with = "vendored-sources"\n'
                    '[source.vendored-sources]\ndirectory = "vendor"\n'
                ),
            )


def verify_materials():
    version = subprocess.check_output(["rustc", "--version"], cwd=SOURCE, text=True)
    if not version.startswith("rustc 1.95.0 "):
        raise ValueError("Use the pinned Rust 1.95.0 toolchain")
    lock = tomllib.loads((SOURCE / "Cargo.lock").read_text("utf-8"))
    locked = {(p["name"], p["version"]): p["checksum"] for p in lock["package"] if "checksum" in p}
    reviewed = json.loads((SOURCE.parent / "dependencies.json").read_text("utf-8"))
    if locked != {(p["name"], p["version"]): p["sha256"] for p in reviewed}:
        raise ValueError("CFB dependency lock differs from its reviewed materials")
    for package in reviewed:
        for name, expected in package["license_files"].items():
            if digest(SOURCE.parent / "licenses" / package["name"] / name) != expected:
                raise ValueError("CFB dependency license material changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "openkb/cfb_helper/assets/runtime")
    parser.add_argument("--check-target", help="Compile-check another target without installing it")
    args = parser.parse_args()
    verify_materials()
    if args.check_target:
        subprocess.run(
            ["cargo", "check", "--locked", "--target", args.check_target], cwd=SOURCE, check=True
        )
        return
    if sys.platform not in {"linux", "win32"}:
        raise ValueError("Native helper supports Linux and Windows")
    subprocess.run(["cargo", "build", "--release", "--locked"], cwd=SOURCE, check=True)
    name = "openkb-cfb.exe" if sys.platform == "win32" else "openkb-cfb"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE / "target/release" / name, output / name)
    material = ["Cargo.lock", "Cargo.toml", "rust-toolchain.toml", "LICENSE"]
    material.extend(
        path.relative_to(SOURCE).as_posix() for path in sorted((SOURCE / "src").rglob("*.rs"))
    )
    source_archive(output / "source.zip", material)
    manifest = {
        "schema_version": 1,
        "platform": sys.platform,
        "version": "openkb-cfb 1.0.0 cfb 0.15.0",
        "binary": name,
        "sha256": digest(output / name),
        "toolchain": "1.95.0",
        "source": {item: digest(SOURCE / item) for item in material},
        "dependencies": digest(SOURCE.parent / "dependencies.json"),
        "source_archive": digest(output / "source.zip"),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Prepared {manifest['version']} for {sys.platform}: {output}")


if __name__ == "__main__":
    main()
