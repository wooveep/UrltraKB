"""The Apple Silicon runtime uses an explicit native platform identity."""

import pytest

pytest_plugins = ("test_office_import",)


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
