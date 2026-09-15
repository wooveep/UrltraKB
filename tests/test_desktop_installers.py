"""Native targets and real Debian package structure, without freezing a fixture app."""

import json
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.desktop_platform import desktop_target
from scripts.package_installers import stage_debian, stage_macos
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


def test_macos_app_preserves_runtime_location(tmp_path):
    program = tmp_path / "program"
    (program / "_internal/openkb").mkdir(parents=True)
    identity = {"version": "0.1.dev123+g123456789abc", "commit": "a" * 40}
    (program / "UrltraKB").write_bytes(b"Mach-O fixture")
    (program / "_internal/openkb/_build_info.json").write_text(json.dumps(identity))
    app = tmp_path / "UrltraKB.app"
    runtime = stage_macos(program, app, identity)
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    assert info["CFBundleExecutable"] == "UrltraKB"
    assert info["CFBundleVersion"] == "123"
    assert info["LSMinimumSystemVersion"] == "13.0"
    assert (runtime / info["CFBundleExecutable"]).read_bytes() == b"Mach-O fixture"
    assert json.loads((runtime / "_internal/openkb/_build_info.json").read_text()) == identity
