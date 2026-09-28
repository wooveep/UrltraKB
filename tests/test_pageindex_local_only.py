"""Removed service credentials and entry points cannot enable remote indexing."""

import importlib.util
import socket

import pytest
from click.testing import CliRunner


@pytest.mark.parametrize("client_name", ["PageIndexClient", "LocalClient"])
def test_service_key_cannot_change_local_storage_or_reading(tmp_path, monkeypatch, client_name):
    import pageindex

    monkeypatch.setenv("PAGEINDEX_API_KEY", "retired-service-key")

    def no_network(*args, **kwargs):
        raise AssertionError("Local indexing and storage must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    client_type = getattr(pageindex, client_name)
    options = {
        "model": "ollama/llama3",
        "storage_path": str(tmp_path / "index"),
        "index_config": {
            "if_add_node_summary": False,
            "if_add_doc_description": False,
            "if_add_node_text": True,
        },
    }
    source = tmp_path / "paper.md"
    source.write_text("# Local paper\nThis document stays in local storage.\n", encoding="utf-8")
    collection = client_type(**options).collection("papers")
    doc_id = collection.add(str(source))
    assert (tmp_path / "index/pageindex.db").is_file()
    assert collection.get_document_structure(doc_id)

    reopened = client_type(**options).collection("papers")
    pages = reopened.get_page_content(doc_id, "1-10")
    assert any("stays in local storage" in page["content"] for page in pages)
    reopened.delete_document(doc_id)
    assert reopened.list_documents() == []


def test_service_sdk_is_unavailable():
    import pageindex

    assert not hasattr(pageindex, "CloudClient")
    assert importlib.util.find_spec("pageindex.backend.cloud") is None
    assert importlib.util.find_spec("pageindex.cloud_api") is None
    assert not hasattr(pageindex.PageIndexClient, "submit_document")
    assert not hasattr(pageindex.PageIndexClient, "chat_completions")
    with pytest.raises(TypeError, match="api_key"):
        pageindex.PageIndexClient(api_key="retired-service-key")
    with pytest.raises(TypeError):
        pageindex.PageIndexClient("retired-service-key")


def test_cli_rejects_retired_service_import_before_ingestion(monkeypatch):
    from openkb.cli import cli

    def unexpected_ingestion(*args, **kwargs):
        raise AssertionError("Retired import option must never start ingestion")

    monkeypatch.setattr("openkb.cli.add_single_file", unexpected_ingestion)
    result = CliRunner().invoke(cli, ["add", "--from-pageindex-cloud", "doc-123"])
    assert result.exit_code == 2
    assert "No such option" in result.output
    assert "--from-pageindex-cloud" not in CliRunner().invoke(cli, ["add", "--help"]).output
