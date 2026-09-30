"""A fixed binary Word file follows the same immutable PDF source contract."""

import shutil
from pathlib import Path

import pymupdf
import pytest

pytest_plugins = ("test_office_import",)

FIXTURES = Path(__file__).parent / "fixtures/office"


def test_binary_word_retains_final_body_blank_page_and_image(
    kb_dir, tmp_path, office_runtime, pdf_model
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    path = tmp_path / "中文 Word 97.doc"
    shutil.copyfile(FIXTURES / "writer.doc", path)
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert (kb_dir / saved["original_path"]).read_bytes() == path.read_bytes()
    assert saved["pages"] == 3
    assert "INSERTED_MARKER" in saved["content"] and "DELETED_MARKER" not in saved["content"]
    assert "HIDDEN_MARKER" not in saved["content"]
    assert saved["office"]["detected_filter"] == "MS Word 97"
    with pymupdf.open(kb_dir / saved["internal_pdf_path"]) as pdf:
        assert pdf[1].get_text().strip() == ""
        image = pdf[2].get_image_info()[0]
        assert (image["width"], image["height"]) == (160, 80)
        assert image["bbox"][1] > pdf[2].search_for("Last section")[0].y0
    path.unlink()
    historical = read_document_source(
        kb_dir, result.source_id, pages="3", source_revision_id=saved["source_revision_id"]
    )
    assert "Last section" in historical["content"]


@pytest.mark.parametrize("kind", ["encrypted", "corrupt", "wrong_format"])
def test_binary_word_failure_keeps_original_and_explains_why(
    kb_dir, tmp_path, writer_document, office_runtime, pdf_model, kind
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    path = tmp_path / "input.doc"
    if kind == "encrypted":
        shutil.copyfile(FIXTURES / "encrypted.doc", path)
    elif kind == "corrupt":
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1broken")
    else:
        shutil.copyfile(writer_document, path)
    result = import_document(kb_dir, path)
    assert result.status == "failed"
    if kind == "encrypted":
        assert "password" in result.message.lower()
    elif kind == "wrong_format":
        assert "filter" in result.message.lower()
    else:
        assert "damaged" in result.message.lower()
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["knowledge_revision_id"] is None
    assert (kb_dir / saved["original_path"]).read_bytes() == path.read_bytes()


def test_binary_word_upload_and_cli_read_the_same_source(kb_dir, office_runtime, pdf_model):
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.cli import cli
    from openkb.config import register_kb_alias

    register_kb_alias("legacy-word", kb_dir)
    original = (FIXTURES / "writer.doc").read_bytes()
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "legacy-word", "stream": "false"},
            files={"files": ("legacy.doc", original)},
        )
    assert response.status_code == 200, response.text
    result = response.json()["files"][0]
    assert result["status"] == "added", result
    read = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "source", result["source_id"]])
    assert read.exit_code == 0, read.output
    saved = json.loads(read.output)
    assert saved["office"]["detected_filter"] == "MS Word 97"
    assert (kb_dir / saved["original_path"]).read_bytes() == original
