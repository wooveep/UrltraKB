"""Public setup operations cannot download on reads or publish incomplete installations."""

import json

import pytest
import requests

from openkb.application.ocr_installation import (
    install_ocr,
    prepare_ocr_install,
    read_ocr_installations,
)
from openkb.application.settings import read_settings_view


@pytest.fixture(autouse=True)
def isolated_global_installation_state(tmp_path, monkeypatch):
    from openkb import config

    root = tmp_path / "global"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", root)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", root / "global.yaml")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_LOCK_PATH", root / "global.lock")


def test_ocr_preparation_reports_pinned_sources_and_does_not_download(kb_dir, monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request", lambda *a, **k: pytest.fail("Unexpected network")
    )
    before = read_settings_view(kb_dir)
    plan = prepare_ocr_install("openvino")
    assert plan["model"] == "PaddleOCR-VL-1.5"
    assert plan["models"]["code_commit"] == "598857c1272c2b7224109ad4573974f7d4cc260f"
    assert plan["models"]["revision"] == "43f60524b332ac2fc5f06aed8f015ad3d0dc000b"
    assert plan["download_bytes"] > 1_882_744_547
    assert len(plan["models"]["files"]) == 17
    assert read_ocr_installations() == []
    assert read_settings_view(kb_dir) == before


def test_incomplete_offline_ocr_package_is_failed_without_network_or_changing_defaults(
    kb_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        requests.Session, "request", lambda *a, **k: pytest.fail("Offline contacted network")
    )
    destination = tmp_path / "shared-ocr"
    offline = tmp_path / "offline"
    offline.mkdir()
    plan = prepare_ocr_install("openvino", destination)
    (offline / "package.json").write_text(
        json.dumps(
            {"id": plan["id"], "profile": "openvino", "platform": plan["runtime"]["platform"]}
        )
    )
    with pytest.raises(ValueError, match="ocr_offline_file_missing_or_changed"):
        install_ocr("openvino", destination=destination, offline=offline)
    rows = read_ocr_installations()
    assert len(rows) == 1 and rows[0]["state"] == "failed"
    assert not list(destination.rglob("ready.json"))
    assert read_settings_view(kb_dir).values.parsing.ocr.backend == "system"


def test_full_pipeline_check_rejects_truncated_sample(kb_dir, tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    from openkb import config
    from openkb.application.ocr_capability import check_ocr_capability
    from openkb.ocr.install_files import digest
    from openkb.ocr.installations import runtime_defaults
    from tests.test_ocr_devices import external_runtime

    assets = tmp_path / "models"
    assets.mkdir()
    (assets / "manifest.json").write_text('{"files":{}}')
    settings = runtime_defaults(Path(sys.executable), assets, "openvino")
    receipt = tmp_path / "ready.json"
    identity = "b" * 64
    receipt.write_text(
        json.dumps(
            {
                "id": identity,
                "settings": settings.model_dump(),
                "manifest_bytes": (assets / "manifest.json").stat().st_size,
            }
        )
    )
    config.GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (config.GLOBAL_CONFIG_DIR / "ocr-installations.json").write_text(
        json.dumps({identity: {"id": identity, "state": "ready", "receipt": str(receipt)}})
    )
    assert settings.assets_sha256 == digest(assets / "manifest.json")
    external_runtime(tmp_path, monkeypatch, "none")
    result = check_ocr_capability(identity, "cpu")
    assert result["status"] == "not_ready"
    assert not list((config.GLOBAL_CONFIG_DIR / "ocr-capabilities").glob("*.json"))
