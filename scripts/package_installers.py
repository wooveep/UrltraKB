"""Make installable CI build artifacts from a verified native program inventory.

These development artifacts keep exact source and build evidence alongside the
installer. Reviewed releases continue to use assemble_distribution/package_desktop.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

try:
    from scripts.desktop_platform import desktop_target
    from scripts.export_desktop_source import verify_source
    from scripts.package_desktop import _program_copy, digest, record
except ModuleNotFoundError:
    from desktop_platform import desktop_target
    from export_desktop_source import verify_source
    from package_desktop import _program_copy, digest, record


DEBIAN_DEPENDS = (
    "libc6 (>= 2.41), libstdc++6, libgcc-s1, libgl1, libegl1, libopengl0, "
    "libglib2.0-0t64, libdbus-1-3, libfontconfig1, libx11-6, libx11-xcb1, "
    "libxext6, libxrender1, libxi6, libxkbcommon0, libxkbcommon-x11-0, "
    "libxcb1, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, "
    "libxcb-randr0, libxcb-render-util0, libxcb-shape0, libxcb-shm0, "
    "libxcb-sync1, libxcb-xfixes0, libxcb-xkb1, zlib1g"
)


def write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(mode)


def stage_debian(program: Path, root: Path, source: Path, identity: dict, arch: str) -> None:
    """Install under /opt with stable launchers, icons and desktop menu integration."""
    installed = root / "opt/urltrakb"
    installed.parent.mkdir(parents=True)
    shutil.move(program, installed)
    for command, executable in {
        "urltrakb": "UrltraKB",
        "urltrakb-cli": "UrltraKBCLI",
        "urltrakb-api": "UrltraKBAPI",
    }.items():
        write(
            root / "usr/bin" / command, f'#!/bin/sh\nexec /opt/urltrakb/{executable} "$@"\n', 0o755
        )
    write(
        root / "usr/share/applications/urltrakb.desktop",
        "[Desktop Entry]\nType=Application\nName=UrltraKB\n"
        "Comment=Local document knowledge base\nExec=urltrakb\nIcon=urltrakb\n"
        "Terminal=false\nCategories=Office;Education;\nStartupWMClass=OpenKB\n",
    )
    icon = root / "usr/share/icons/hicolor/scalable/apps/urltrakb.svg"
    icon.parent.mkdir(parents=True)
    shutil.copy2(source / "openkb/desktop/assets/brand/openkb-app-icon.svg", icon)
    write(root / "usr/share/doc/urltrakb/copyright", (source / "LICENSE").read_text("utf-8"))
    # PEP 440's .dev must sort before the corresponding Debian release.
    version = identity["version"].replace(".dev", "~dev")
    size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) // 1024 + 1
    write(
        root / "DEBIAN/control",
        f"Package: urltrakb\nVersion: {version}\nArchitecture: {arch}\n"
        "Maintainer: UrltraKB maintainers <noreply@github.com>\n"
        "Section: utils\nPriority: optional\n"
        f"Installed-Size: {size}\nDepends: {DEBIAN_DEPENDS}\n"
        "Homepage: https://github.com/wooveep/UrltraKB\n"
        "Description: UrltraKB native knowledge-base workbench\n"
        " Includes desktop, CLI and REST entry points and their Python runtime.\n",
    )


def stage_macos(program: Path, app: Path, identity: dict) -> Path:
    """Keep the inventoried onedir runtime intact inside the native app bundle."""
    contents = app / "Contents"
    contents.mkdir(parents=True)
    runtime = contents / "MacOS"
    shutil.move(program, runtime)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleExecutable": "UrltraKB",
                "CFBundleIdentifier": "io.github.wooveep.urltrakb",
                "CFBundleName": "UrltraKB",
                "CFBundleDisplayName": "UrltraKB",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "0.1.0",
                "CFBundleVersion": identity["version"].split(".dev")[1].split("+")[0],
                "LSMinimumSystemVersion": "14.0",
                "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
            },
            stream,
        )
    return runtime


def source_archive(source: Path, target: Path) -> None:
    export = json.loads((source / "source-export.json").read_text("utf-8"))
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted({*export["files"], "source-export.json"}):
            archive.write(source / name, "program-source/" + name)


def package(source: Path, program: Path, inventory: dict, output: Path) -> Path:
    identity = verify_source(source)
    target = desktop_target(inventory["platform"]["system"], inventory["platform"]["machine"])
    if target != desktop_target():
        raise ValueError("Installer packaging must run on the inventoried native host")
    output.mkdir(parents=True, exist_ok=True)
    stem = f"UrltraKB-{identity['version']}-{target.name}"
    extension = ".deb" if target.deb_arch else ".zip"
    names = [stem + extension, stem + "-source.zip", stem + "-build.json"]
    if any((output / name).exists() for name in names):
        raise FileExistsError("Build artifacts already exist; select a fresh output directory")
    with tempfile.TemporaryDirectory(prefix="installer-", dir=output) as temporary:
        work = Path(temporary)
        staged = work / "UrltraKB"
        _program_copy(program, inventory, staged, identity)
        source_archive(source, work / names[1])
        source_record = record(work / names[1], "source")
        write(
            staged / "BUILD-NOTICE.txt",
            f"UrltraKB development build {identity['version']}\nCommit: {identity['commit']}\n"
            f"Target: {target.name}\nSource: {source_record.name}\n"
            f"Source SHA256: {source_record.sha256}\n"
            "Built automatically from locked inputs. This is a CI test artifact.\n"
            "Reviewed source/license release materials are assembled separately using\n"
            "packaging/desktop/DISTRIBUTION.md. Optional OCR runtimes are installed separately.\n",
        )
        shutil.copy2(source / "LICENSE", staged / "LICENSE")
        artifact = work / names[0]
        if target.deb_arch:
            root = work / "debian"
            stage_debian(staged, root, source, identity, target.deb_arch)
            subprocess.run(
                ["dpkg-deb", "--build", "--root-owner-group", "-Zxz", str(root), str(artifact)],
                check=True,
            )
            # Exercise actual extraction and validate the packaged payload, not just staging.
            unpacked = work / "unpacked"
            subprocess.run(["dpkg-deb", "--extract", str(artifact), str(unpacked)], check=True)
            installed = unpacked / "opt/urltrakb"
        elif target.system == "Darwin":
            app = work / "UrltraKB.app"
            stage_macos(staged, app, identity)
            subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
            subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
            subprocess.run(
                ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(artifact)],
                check=True,
            )
            unpacked = work / "unpacked"
            subprocess.run(["ditto", "-x", "-k", str(artifact), str(unpacked)], check=True)
            installed = unpacked / "UrltraKB.app/Contents/MacOS"
        else:
            with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(staged.rglob("*")):
                    if path.is_file():
                        archive.write(path, "UrltraKB/" + path.relative_to(staged).as_posix())
            unpacked = work / "unpacked"
            with zipfile.ZipFile(artifact) as archive:
                archive.extractall(unpacked)
            installed = unpacked / "UrltraKB"
        # Signing changes Mach-O signatures; compare extraction against the signed staging tree.
        original = (
            work / "UrltraKB.app/Contents/MacOS"
            if target.system == "Darwin"
            else work / "debian/opt/urltrakb"
            if target.deb_arch
            else staged
        )
        for path in original.rglob("*"):
            if path.is_file() and digest(path) != digest(installed / path.relative_to(original)):
                raise ValueError(f"Installer content differs from staging: {path.name}")
        suffix = ".exe" if target.system == "Windows" else ""
        subprocess.run([str(installed / ("UrltraKBCLI" + suffix)), "--help"], check=True)
        subprocess.run(
            [
                str(installed / ("UrltraKBVerify" + suffix)),
                "--output",
                str(output / (stem + "-check")),
            ],
            check=True,
            timeout=300,
            env=dict(os.environ),
        )
        evidence = {
            **identity,
            "target": target.name,
            "scope": "CI build; release materials unaudited",
            "inventory": inventory,
            "installer_sha256": digest(artifact),
            "source_sha256": source_record.sha256,
            "macos_signing": "ad-hoc; not notarized" if target.system == "Darwin" else None,
        }
        write(work / names[2], json.dumps(evidence, indent=2) + "\n")
        if verify_source(source) != identity:
            raise ValueError("Source changed while packaging")
        for name in names:
            (work / name).rename(output / name)
    return output / names[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "program", "inventory", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = package(
        args.source.resolve(),
        args.program.resolve(),
        json.loads(args.inventory.read_text("utf-8")),
        args.output.resolve(),
    )
    print(result)


if __name__ == "__main__":
    main()
