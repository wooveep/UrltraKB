"""Native targets and real Debian package structure, without freezing a fixture app."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.desktop_platform import desktop_target
from scripts.macos_bundle import bundle_toc, info_plist
from scripts.package_installers import stage_debian
from scripts.prepare_desktop_assets import NODE_HASHES


@pytest.mark.parametrize(
    ("system", "machine", "name", "archive"),
    [
        ("Linux", "x86_64", "debian-amd64", "linux-x64.tar.xz"),
        ("Linux", "aarch64", "debian-arm64", "linux-arm64.tar.xz"),
        ("Windows", "AMD64", "windows-x64", "win-x64.zip"),
        ("Darwin", "arm64", "macos-arm64", "darwin-arm64.tar.gz"),
    ],
)
def test_runtime_download_matches_native_host(system, machine, name, archive):
    target = desktop_target(system, machine)
    assert target.name == name
    assert target.node_archive == archive
    assert len(NODE_HASHES[archive]) == 64


@pytest.mark.parametrize("host", [("Linux", "i686"), ("Windows", "x86"), ("Darwin", "x86_64")])
def test_unsupported_host_fails_explicitly(host):
    with pytest.raises(ValueError, match="Unsupported desktop host"):
        desktop_target(*host)


@pytest.mark.skipif(not shutil.which("dpkg-deb"), reason="native Debian packaging tool")
@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_debian_metadata_and_extracted_installation(tmp_path, arch):
    program = tmp_path / "program"
    (program / "_internal").mkdir(parents=True)
    (program / "UrltraKB").write_text("#!/bin/sh\nexit 0\n")
    (program / "UrltraKB").chmod(0o755)
    (program / "_internal/lib.so.1").write_bytes(b"library")
    (program / "_internal/lib.so").symlink_to("lib.so.1")
    source = Path(__file__).resolve().parents[1]
    root = tmp_path / "package"
    stage_debian(program, root, source, {"version": "0.1.dev123+g123456789abc"}, arch)
    deb = tmp_path / "test.deb"
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(deb)], check=True)
    assert (
        subprocess.check_output(["dpkg-deb", "-f", str(deb), "Architecture"], text=True).strip()
        == arch
    )
    assert (
        subprocess.check_output(["dpkg-deb", "-f", str(deb), "Version"], text=True).strip()
        == "0.1~dev123+g123456789abc"
    )
    installed = tmp_path / "installed"
    subprocess.run(["dpkg-deb", "--extract", str(deb), str(installed)], check=True)
    assert (installed / "opt/urltrakb/UrltraKB").stat().st_mode & 0o111
    assert (installed / "opt/urltrakb/_internal/lib.so").is_symlink()
    assert (installed / "opt/urltrakb/_internal/lib.so").read_bytes() == b"library"
    assert 'exec /opt/urltrakb/UrltraKB "$@"' in (installed / "usr/bin/urltrakb").read_text()
    assert "Exec=urltrakb\n" in (installed / "usr/share/applications/urltrakb.desktop").read_text()


def test_macos_bundle_keeps_metadata_out_of_code_directories(tmp_path):
    osx = pytest.importorskip("PyInstaller.building.osx")
    program = tmp_path / "program"
    identity = {"version": "0.1.dev123+g123456789abc", "commit": "a" * 40}
    inventory = {
        "files": [
            {"path": "UrltraKB"},
            {"path": "_internal/coloredlogs-15.0.1.dist-info/METADATA", "typecode": "DATA"},
            {"path": "_internal/library.dylib", "typecode": "BINARY"},
            {"path": "_internal/openkb/_build_info.json", "typecode": "DATA"},
        ]
    }
    # Exercise PyInstaller's real layout logic; copying an onedir tree into MacOS
    # makes codesign mistake Python .dist-info directories for unsigned bundles.
    entries = osx.BUNDLE.__new__(osx.BUNDLE)._process_bundle_toc(bundle_toc(program, inventory))
    layout = {name: (source, kind) for name, source, kind in entries}
    assert layout["Contents/MacOS/UrltraKB"][1] == "EXECUTABLE"
    assert layout["Contents/Frameworks/library.dylib"][1] == "BINARY"
    assert layout["Contents/Resources/coloredlogs-15.0.1.dist-info/METADATA"][1] == "DATA"
    assert layout["Contents/Frameworks/coloredlogs-15.0.1.dist-info"][1] == "SYMLINK"
    assert layout["Contents/Resources/openkb/_build_info.json"][1] == "DATA"
    assert info_plist(identity)["LSMinimumSystemVersion"] == "14.0"


def test_program_copy_preserves_mac_framework_links(tmp_path):
    from scripts.package_desktop import _program_copy, digest

    original = tmp_path / "program"
    framework = original / "_internal/QtCore.framework/Versions"
    (framework / "A/Resources").mkdir(parents=True)
    (framework / "A/Resources/Info.plist").write_bytes(b"framework metadata")
    (framework / "Current").symlink_to("A", target_is_directory=True)
    (framework.parent / "Resources").symlink_to(
        "Versions/Current/Resources", target_is_directory=True
    )
    identity = {"version": "0.1.dev123+g123456789abc", "commit": "a" * 40}
    info = original / "_internal/openkb/_build_info.json"
    info.parent.mkdir()
    info.write_text(json.dumps(identity))
    for name in ("UrltraKB", "UrltraKBCLI", "UrltraKBAPI", "UrltraKBVerify"):
        (original / name).write_bytes(b"executable")
        (original / name).chmod(0o755)
    inventory = {
        **identity,
        "platform": {"system": "Darwin", "machine": "arm64"},
        "files": [
            {
                "path": p.relative_to(original).as_posix(),
                "size": p.stat().st_size,
                "sha256": digest(p),
            }
            for p in original.rglob("*")
            if p.is_file()
        ],
        "directory_links": {
            "_internal/QtCore.framework/Resources": "Versions/Current/Resources",
            "_internal/QtCore.framework/Versions/Current": "A",
        },
    }
    copied = tmp_path / "copied"
    _program_copy(original, inventory, copied, identity)
    resources = copied / "_internal/QtCore.framework/Resources"
    assert resources.is_symlink()
    assert (resources / "Info.plist").read_bytes() == b"framework metadata"
    assert resources.resolve().is_relative_to(copied)
