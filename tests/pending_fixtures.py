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
