"""The independent HTTP product retains artifacts without serving the retired UI."""

from fastapi.testclient import TestClient

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
