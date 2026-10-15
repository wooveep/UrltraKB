"""The Apple Silicon runtime uses an explicit native platform identity."""

import sys

import pytest

pytest_plugins = ("test_office_import",)


@pytest.mark.parametrize("layout", ["app", "onedir"])
def test_frozen_application_finds_office_after_bundle_relocation(
    kb_dir, tmp_path, monkeypatch, layout
):
    from ultrakb.office.runtime import runtime_path

    install = tmp_path / "installation"
    if layout == "app":
        executable = install / "UltraKB.app/Contents/MacOS/UltraKB"
        internal = executable.parent.parent / "Frameworks"
        office = executable.parent.parent / "Resources/office"
        internal.mkdir(parents=True)
        office.mkdir(parents=True)
        (internal / "office").symlink_to("../Resources/office", target_is_directory=True)
    else:
        executable = install / "UltraKB"
        internal = install / "_internal"
        office = internal / "office"
        office.mkdir(parents=True)
    (office / "ultrakb-office.json").write_text("runtime inventory")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert (runtime_path(kb_dir) / "ultrakb-office.json").read_text() == "runtime inventory"


def test_private_runtime_converts_word_and_powerpoint(
    kb_dir, office_runtime, tmp_path, monkeypatch
):
    from ultrakb.desktop.verification_runtimes import verify_office_conversions
    from ultrakb.office import convert

    run = convert.run_supervised

    def capture_failure(root, python, launcher, directory, *args, **kwargs):
        try:
            return run(root, python, launcher, directory, *args, **kwargs)
        except Exception:
            print((directory / "process.log").read_text(errors="replace"))
            raise

    monkeypatch.setattr(convert, "run_supervised", capture_failure)

    assert len(verify_office_conversions(kb_dir, tmp_path)) == 4


def test_office_accepts_apple_silicon_host(monkeypatch):
    from ultrakb.office import probe

    monkeypatch.setattr(probe.sys, "platform", "darwin")
    monkeypatch.setattr(probe.platform, "machine", lambda: "arm64")
    details = probe.host_conditions()
    assert details["platform"] == "darwin"
    assert details["architecture"] == "arm64"


def test_office_rejects_unshipped_intel_mac_runtime(monkeypatch):
    from ultrakb.office import probe

    monkeypatch.setattr(probe.sys, "platform", "darwin")
    monkeypatch.setattr(probe.platform, "machine", lambda: "x86_64")
    with pytest.raises(ValueError, match="Office runtime requires"):
        probe.host_conditions()
