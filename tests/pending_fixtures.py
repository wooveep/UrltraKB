"""Small real packages for durable discovery tests."""

from zipfile import ZipFile

import pytest

pytest_plugins = ("test_office_import",)


@pytest.fixture
def embedded_docx(writer_document):
    payload = writer_document.read_bytes()
    with ZipFile(writer_document, "a") as archive:
        archive.writestr("word/embeddings/Recovered.docx", payload)
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="one" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/package" Target="embeddings/Recovered.docx"/>'
            '<Relationship Id="two" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/package" Target="embeddings/Recovered.docx"/>'
            "</Relationships>",
        )
    return writer_document


@pytest.fixture
def prepared_cfb_helper():
    from pathlib import Path

    from openkb.cfb_helper.runtime import helper_path

    root = Path(__file__).parents[1] / "openkb/cfb_helper/assets/runtime"
    if not (root / "manifest.json").exists():
        pytest.skip("Real CFB recovery needs scripts/prepare_cfb_helper.py")
    return helper_path()
