"""The Apple Silicon runtime uses an explicit native platform identity."""

import sys

import pytest

pytest_plugins = ("test_office_import",)


@pytest.mark.parametrize("layout", ["app", "onedir"])
def test_frozen_application_finds_office_after_bundle_relocation(
    kb_dir, tmp_path, monkeypatch, layout
):
    from openkb.office.runtime import runtime_path

    install = tmp_path / "installation"
    if layout == "app":
        executable = install / "UrltraKB.app/Contents/MacOS/UrltraKB"
        internal = executable.parent.parent / "Frameworks"
        office = executable.parent.parent / "Resources/office"
        internal.mkdir(parents=True)
        office.mkdir(parents=True)
        (internal / "office").symlink_to("../Resources/office", target_is_directory=True)
    else:
        executable = install / "UrltraKB"
        internal = install / "_internal"
        office = internal / "office"
        office.mkdir(parents=True)
    (office / "openkb-office.json").write_text("runtime inventory")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert (runtime_path(kb_dir) / "openkb-office.json").read_text() == "runtime inventory"


def test_private_runtime_converts_word_and_powerpoint(kb_dir, office_runtime, tmp_path):
    from openkb.desktop.verification_runtimes import verify_office_conversions

    assert len(verify_office_conversions(kb_dir, tmp_path)) == 4


def test_office_accepts_apple_silicon_host(monkeypatch):
    from openkb.office import probe

    monkeypatch.setattr(probe.sys, "platform", "darwin")
    monkeypatch.setattr(probe.platform, "machine", lambda: "arm64")
    details = probe.host_conditions()
    assert details["platform"] == "darwin"
    assert details["architecture"] == "arm64"


def test_office_rejects_unshipped_intel_mac_runtime(monkeypatch):
    from openkb.office import probe

    monkeypatch.setattr(probe.sys, "platform", "darwin")
    monkeypatch.setattr(probe.platform, "machine", lambda: "x86_64")
    with pytest.raises(ValueError, match="Office runtime requires"):
        probe.host_conditions()
