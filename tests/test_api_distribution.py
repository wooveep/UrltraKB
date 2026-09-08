"""The independent HTTP product retains artifacts without serving the retired UI."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from openkb import __version__
from openkb.api import create_app


def test_api_ignores_stale_browser_bundle_and_still_delivers_html(kb_dir, tmp_path, monkeypatch):
    # Upgrading an old source/install directory must not revive its browser UI.
    package = tmp_path / "stale-package"
    web = package / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<html>Retired product</html>", encoding="utf-8")
    monkeypatch.setattr("openkb.api_helpers.__file__", str(package / "api_helpers.py"))
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    monkeypatch.setenv("OPENKB_KB_ROOT", str(kb_dir.parent))
    deck = kb_dir / "output/decks/example/index.html"
    deck.parent.mkdir(parents=True)
    deck.write_text("<html>Generated deck</html>", encoding="utf-8")

    with TestClient(create_app()) as client:
        assert client.get("/").status_code == 404
        assert client.get("/index.html").status_code == 404
        unknown = client.get("/api/v1/not-an-endpoint")
        assert unknown.status_code == 404
        assert unknown.json() == {"detail": "Not found"}
        assert client.get("/api/v1/meta").status_code == 200
        artifact = client.get("/api/v1/deck/example", params={"kb": kb_dir.name})
        assert artifact.status_code == 200
        assert artifact.headers["content-type"].startswith("text/html")
        assert artifact.text == "<html>Generated deck</html>"


def _release(tmp_path, monkeypatch):
    root = tmp_path / "distribution"
    root.mkdir()
    files = []
    for kind, name, content in (
        ("source", "openkb-source.tar.gz", b"matching corresponding source"),
        ("licenses", "licenses.zip", b"complete original licenses"),
        ("notice", "NOTICE.txt", b"OpenKB combined distribution; original licenses retained"),
        ("components", "components.json", b"[]"),
        ("build", "build.json", b"{}"),
    ):
        (root / name).write_bytes(content)
        files.append(
            {
                "kind": kind,
                "name": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = {"schema": 1, "version": __version__, "commit": "a" * 40, "files": files}
    (root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    package = tmp_path / "installed-package"
    package.mkdir()
    (package / "_build_info.json").write_text(
        json.dumps({"version": __version__, "commit": manifest["commit"]}), encoding="utf-8"
    )
    monkeypatch.setattr("openkb.distribution.__file__", str(package / "distribution.py"))
    monkeypatch.setenv("OPENKB_DISTRIBUTION_DIR", str(root))
    return root, manifest


def test_remote_users_can_find_and_download_matching_source_without_kb_access(
    tmp_path, monkeypatch
):
    root, manifest = _release(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENKB_API_TOKEN", "private-test-token")
    with TestClient(create_app()) as client:
        private = client.get("/api/v1/meta")
        assert private.status_code == 401
        assert "/api/v1/distribution" in private.headers["link"]
        response = client.get("/api/v1/distribution")
        assert response.status_code == 200
        assert response.json()["commit"] == manifest["commit"]
        assert str(root) not in response.text  # Never advertise server-local paths.
        for file in response.json()["files"]:
            download = client.get(file["download"])
            assert download.status_code == 200
            assert hashlib.sha256(download.content).hexdigest() == file["sha256"]
        assert client.get("/api/v1/distribution/files/release.json").status_code == 404
        assert client.get("/api/v1/distribution/files/.env").status_code == 404


def test_distribution_refuses_damaged_or_redirected_materials(tmp_path, monkeypatch):
    root, manifest = _release(tmp_path, monkeypatch)
    source = root / "openkb-source.tar.gz"
    source.write_bytes(b"different source")
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/distribution/files/openkb-source.tar.gz").status_code == 503
        manifest["files"][0]["name"] = "../private.txt"
        (root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert client.get("/api/v1/distribution").status_code == 503


def test_source_checkout_does_not_claim_complete_distribution_materials(monkeypatch):
    monkeypatch.delenv("OPENKB_DISTRIBUTION_DIR", raising=False)
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/distribution")
        assert response.status_code == 200
        assert response.json()["status"] == "development"
        assert response.json()["files"] == []


def test_runtime_package_keeps_licenses_and_identifies_separate_source(tmp_path, monkeypatch):
    root, manifest = _release(tmp_path, monkeypatch)
    source = manifest["files"][0]
    (root / source["name"]).unlink()
    manifest.update(schema=2, source_archive={**source, "name": "UrltraKB-materials.zip"})
    manifest["files"] = [f for f in manifest["files"] if f["kind"] in {"licenses", "notice"}]
    (root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("OPENKB_API_TOKEN", "private-test-token")
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/distribution")
        assert response.status_code == 200
        assert response.json()["source_archive"] == manifest["source_archive"]
        assert {f["kind"] for f in response.json()["files"]} == {"licenses", "notice"}
        for file in response.json()["files"]:
            assert client.get(file["download"]).status_code == 200
        assert client.get("/api/v1/distribution/files/UrltraKB-materials.zip").status_code == 404
        manifest["source_archive"]["name"] = "../private.zip"
        (root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert client.get("/api/v1/distribution").status_code == 503


def test_distribution_requires_installed_commit_identity(tmp_path, monkeypatch):
    root, manifest = _release(tmp_path, monkeypatch)
    manifest["commit"] = "b" * 40
    (root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/distribution").status_code == 503


def test_public_download_uses_verified_bytes_when_live_file_changes(tmp_path, monkeypatch):
    from openkb.distribution import Distribution

    root, _ = _release(tmp_path, monkeypatch)
    name = "openkb-source.tar.gz"
    original = (root / name).read_bytes()
    verify = Distribution.open_file

    def replace_after_verification(self, name):
        result = verify(self, name)
        (root / name).write_bytes(b"Changed after verification; must never be delivered")
        return result

    monkeypatch.setattr(Distribution, "open_file", replace_after_verification)
    with TestClient(create_app()) as client:
        response = client.get(f"/api/v1/distribution/files/{name}")
        assert response.status_code == 200
        assert response.content == original


def test_public_download_closes_snapshot_when_client_disconnects(tmp_path, monkeypatch):
    from openkb.distribution import Distribution

    _release(tmp_path, monkeypatch)
    opened = []
    original = Distribution.open_file

    def record(self, name):
        stream = original(self, name)
        opened.append(stream)
        return stream

    monkeypatch.setattr(Distribution, "open_file", record)
    app = create_app()

    async def disconnect():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            raise OSError("Client disconnected before receiving headers")

        await app(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "root_path": "",
                "path": "/api/v1/distribution/files/openkb-source.tar.gz",
                "headers": [],
                "query_string": b"",
                "http_version": "1.1",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
            },
            receive,
            send,
        )

    with pytest.raises(ClientDisconnect):
        asyncio.run(disconnect())
    assert len(opened) == 1 and opened[0].closed


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX special files")
def test_special_file_manifest_is_rejected_without_blocking(tmp_path, monkeypatch):
    root, _ = _release(tmp_path, monkeypatch)
    (root / "release.json").unlink()
    os.mkfifo(root / "release.json")
    script = """
from openkb.distribution import DistributionError, load_distribution
try:
    load_distribution()
except DistributionError:
    pass
else:
    raise AssertionError('Special manifest accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
