"""Catalog discovery accepts draft credentials and never forwards a saved key elsewhere."""

from types import SimpleNamespace

import httpx
import pytest

from ultrakb.application import model_catalog as catalog
from ultrakb.config import LlmCredentialBundle


@pytest.fixture
def connection(monkeypatch):
    value = SimpleNamespace(
        model="openai/example",
        credentials=LlmCredentialBundle(
            api_key="saved-private-key", base_url="https://saved.example/v1"
        ),
    )
    monkeypatch.setattr(catalog, "capture_connection", lambda _: value)
    return value


def transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        catalog.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_catalog_uses_draft_key_and_preserves_complete_model_ids(connection, monkeypatch):
    def respond(request):
        assert request.url.path == "/v1/models"
        assert request.headers.get("Authorization") == "Bearer draft-private-key"
        return httpx.Response(200, json={"data": [{"id": "org/model"}, {"id": "b"}, {"id": "b"}]})

    transport(monkeypatch, respond)
    request = catalog.ModelCatalogRequest(
        base_url="https://new.example/v1", api_key="draft-private-key"
    )
    assert "draft-private-key" not in repr(request)
    assert catalog.read_model_catalog(request).models == ("b", "org/model")


def test_catalog_refuses_to_reuse_saved_key_at_another_endpoint(connection, monkeypatch):
    transport(
        monkeypatch, lambda _: pytest.fail("Saved credentials must stay at their own endpoint")
    )
    result = catalog.read_model_catalog(
        catalog.ModelCatalogRequest(base_url="https://other.example/v1")
    )
    assert not result.models and "API Key" in result.message


def test_local_catalog_omits_external_authorization(connection, monkeypatch):
    def respond(request):
        assert "Authorization" not in request.headers
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen3:latest"}]})

    transport(monkeypatch, respond)
    result = catalog.read_model_catalog(
        catalog.ModelCatalogRequest(base_url="http://localhost:11434", local=True)
    )
    assert result.models == ("qwen3:latest",)


@pytest.mark.parametrize("payload", [{"data": "invalid"}, {"data": [None]}, {"data": [{"id": 9}]}])
def test_catalog_rejects_invalid_provider_shapes(connection, monkeypatch, payload):
    transport(monkeypatch, lambda _: httpx.Response(200, json=payload))
    result = catalog.read_model_catalog(
        catalog.ModelCatalogRequest(base_url="https://saved.example/v1")
    )
    assert not result.models and "手动填写" in result.message


def test_catalog_provider_error_is_actionable_and_secret_free(connection, monkeypatch):
    transport(monkeypatch, lambda _: httpx.Response(401, text="saved-private-key"))
    result = catalog.read_model_catalog(
        catalog.ModelCatalogRequest(base_url="https://saved.example/v1")
    )
    assert not result.models and "saved-private-key" not in result.message
    assert "手动填写" in result.message
