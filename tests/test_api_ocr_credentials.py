"""Cloud OCR credential edits use the same public settings contract as model keys."""

import os

import pytest
from fastapi.testclient import TestClient

from openkb.api import create_app


@pytest.mark.parametrize("scope", ["global", "kb"])
def test_rest_ocr_key_is_write_only_and_invalid_patch_keeps_settings(
    scope, kb_dir, tmp_path, monkeypatch, caplog
):
    from openkb import config

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    monkeypatch.setenv("OPENKB_KB_ROOT", str(kb_dir.parent))
    monkeypatch.setenv("OPENKB_API_TOKEN", "local-test-token")
    monkeypatch.delenv("PADDLEOCR_API_KEY", raising=False)
    client = TestClient(create_app())
    url = "/api/v1/config" if scope == "global" else "/api/v1/kb/config"
    selector = {} if scope == "global" else {"kb": kb_dir.name}
    auth = {"Authorization": "Bearer local-test-token"}
    secret = "rest-ocr-fixture-key"
    saved = client.patch(url, json={**selector, "ocr_api_key": secret}, headers=auth)
    assert saved.status_code == 200 and saved.json()["has_ocr_api_key"]
    assert secret not in saved.text and secret not in caplog.text
    original = client.get(url, params=selector, headers=auth).json()
    assert secret not in str(original)
    failed = client.patch(
        url,
        json={**selector, "ocr_api_key": "bad\nINJECTED=key", "config": {"language": "changed"}},
        headers=auth,
    )
    assert failed.status_code == 400 and "INJECTED" not in failed.text
    assert client.get(url, params=selector, headers=auth).json() == original
    env_path = (config.GLOBAL_CONFIG_DIR if scope == "global" else kb_dir) / ".env"
    if os.name == "posix":
        assert env_path.stat().st_mode & 0o777 == 0o600
    cleared = client.patch(url, json={**selector, "ocr_api_key": None}, headers=auth)
    assert cleared.status_code == 200 and not cleared.json()["has_ocr_api_key"]
