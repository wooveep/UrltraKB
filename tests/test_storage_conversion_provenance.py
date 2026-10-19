"""Retained conversion provenance belongs to the exact native source revision."""

import hashlib

import pytest
from test_pending_empty_html import html_model_transport  # noqa: F401

pytest_plugins = ("pending_fixtures",)
pytestmark = pytest.mark.integration


def test_office_reader_keeps_the_conversion_receipt_and_pdf_after_external_input_is_gone(
    kb_dir, writer_document, monkeypatch
):
    from native_catalog_fixtures import catalog

    from ultrakb.application.documents import import_document
    from ultrakb.application.recompilation import recompile_document
    from ultrakb.documents import read_document_source

    original = writer_document.read_bytes()
    imported = import_document(kb_dir, writer_document)
    assert imported.status == "added", imported.message
    before = read_document_source(kb_dir, imported.source_id)
    assert before["office"]["filter"] == "writer_pdf_Export"
    assert before["office"]["runtime_fingerprint"]
    store = catalog(kb_dir)
    unit = store.unit_revision(before["unit_revision_id"])
    processing = store.processing(unit.processing_id)
    [pdf] = [file for file in processing.files if file.path.endswith(".pdf")]
    pdf_bytes = store.read_processed(processing.processing_id, pdf.path)
    assert hashlib.sha256(pdf_bytes).hexdigest() == before["office"]["pdf_digest"]
    assert pdf_bytes.startswith(b"%PDF")
    assert store.read_original(before["source_revision_id"]) == original
    writer_document.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("Recompilation must use retained PDF input and provenance")

    monkeypatch.setattr("ultrakb.office.convert.convert_office", forbidden)
    import asyncio

    result = asyncio.run(recompile_document(kb_dir, imported.source_id, unit_id=unit.unit_id))
    assert result.status == "compiled", result.message
    after = read_document_source(kb_dir, imported.source_id)
    assert after["office"] == before["office"]
    assert after["unit_revision_id"] == before["unit_revision_id"]
    assert after["content"] == before["content"]


@pytest.mark.parametrize("field", ["input_digest", "pdf_digest"])
def test_retained_conversion_receipt_cannot_be_rebound_to_another_input(
    kb_dir, writer_document, tmp_path, field
):
    import json

    from native_catalog_fixtures import catalog

    from ultrakb.application.documents import import_document
    from ultrakb.documents import read_document_source

    imported = import_document(kb_dir, writer_document)
    assert imported.status == "added", imported.message
    before = read_document_source(kb_dir, imported.source_id)
    store = catalog(kb_dir)
    unit = store.unit_revision(before["unit_revision_id"])
    processing = store.processing(unit.processing_id)
    files = {}
    for file in processing.files:
        content = store.read_processed(processing.processing_id, file.path)
        if file.path.endswith(".office.json"):
            receipt = json.loads(content)
            receipt[field] = "f" * 64
            content = json.dumps(receipt).encode()
        path = tmp_path / "altered-processing" / file.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files[file.path] = path
    altered = store.retain_processing(
        unit.source_revision_id,
        files,
        body_path=processing.body_path,
        source_path=processing.source_path,
        source_kind=processing.source_kind,
        fingerprint="altered-receipt-" + field,
        operation_id="altered-receipt-" + field,
        decision=processing.decision,
    )
    target = store.plan_unit(
        altered.processing_id,
        key=unit.key,
        doc_name=unit.doc_name,
        expected_generation=unit.generation,
    )
    with pytest.raises(ValueError, match="provenance differs"):
        read_document_source(kb_dir, unit.source_id, unit_revision_id=target.unit_revision_id)
    assert read_document_source(kb_dir, unit.source_id)["office"] == before["office"]
