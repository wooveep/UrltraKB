"""Retired cloud configuration cannot route local navigation to a remote backend."""

import pymupdf
from click.testing import CliRunner


def test_old_cloud_cli_option_is_unavailable():
    from openkb.cli import cli

    result = CliRunner().invoke(cli, ["add", "--from-pageindex-cloud", "old-document"])
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_old_environment_key_cannot_enable_pageindex_cloud(
    kb_dir, tmp_path, monkeypatch, model_service
):
    from pageindex.backend.cloud import CloudBackend
    from pageindex.collection import Collection

    from openkb.indexer import index_long_document

    monkeypatch.setenv("PAGEINDEX_API_KEY", "retired-setting")

    def forbidden(*args, **kwargs):
        raise AssertionError("Retired cloud backend was instantiated")

    monkeypatch.setattr(CloudBackend, "__init__", forbidden)
    monkeypatch.setattr(Collection, "add", lambda *args, **kwargs: "local-id")
    monkeypatch.setattr(
        Collection,
        "get_document",
        lambda *args, **kwargs: {
            "doc_name": "native",
            "doc_description": "Local navigation",
            "structure": [],
        },
    )
    source = tmp_path / "native.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((40, 40), "Native text")
        pdf.save(source)
    result = index_long_document(source, kb_dir)
    assert result.doc_id == "local-id"
    assert (kb_dir / "wiki/sources/native.json").is_file()
