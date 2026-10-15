import io
from zipfile import ZipFile

from openpyxl import Workbook

pytest_plugins = ("pending_fixtures", "processing_fixtures")


def test_saved_94_cursor_retains_its_policy_and_completed_independent_source(
    kb_dir,
    writer_document,
    pdf_model,
    office_runtime,
    processing_only,
    processing_context,
    live_llm,
):
    import json

    from processing_fixtures import finish_recovered_sources, publish_processed_source

    from ultrakb.application.pending import pending_status, process_pending, update_execution_budget

    payload = writer_document.read_bytes()
    with ZipFile(writer_document, "a") as package:
        for name in ("word/embeddings/A.docx", "word/embeddings/B.docx", "custom/new.docx"):
            package.writestr(name, payload)
        package.writestr(
            "word/_rels/document.xml.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="new" Type="https://example.invalid/package" '
                'Target="../custom/new.docx"/></Relationships>'
            ),
        )
    calls_before = len(live_llm.calls)
    parent = processing_only(writer_document)
    initial = pending_status(kb_dir)
    job = initial["jobs"][0]
    # Model a saved #94 admission, whose discovery sequence is immutable.
    path = kb_dir / ".ultrakb/catalog/discovery-intents" / (job["id"] + ".json")
    record = json.loads(path.read_text())
    record["policy"] = "docx-embedded-package-v1"
    path.write_text(json.dumps(record))
    update_execution_budget(kb_dir, job["root_import_id"], {"max_sources": 2})
    process_pending(kb_dir, max_jobs=1, context=processing_context)
    assert pending_status(kb_dir)["jobs"][0]["cursor"] == 1
    process_pending(kb_dir, max_jobs=1, context=processing_context)
    staged = next(j for j in pending_status(kb_dir)["jobs"] if j["kind"] == "import")
    published = publish_processed_source(kb_dir, staged["source_id"])
    # The real pending claimant reconciles the durable publication checkpoint.
    process_pending(kb_dir, context=processing_context)
    done = next(j for j in pending_status(kb_dir)["jobs"] if j["kind"] == "import")
    assert done["status"] == "completed"
    update_execution_budget(kb_dir, job["root_import_id"], {"max_sources": 10})
    finish_recovered_sources(kb_dir, parent.source_id, processing_context)
    after = pending_status(kb_dir)
    assert next(j for j in after["jobs"] if j["id"] == done["id"])["source_id"] == done["source_id"]
    assert after["groups"][0]["sources"] == 3
    assert {c["object_key"] for c in after["checkpoints"]} == {
        "word/embeddings/A.docx",
        "word/embeddings/B.docx",
    }
    from ultrakb.unit_publication import read_unit_publication

    assert (
        read_unit_publication(kb_dir, published.unit_id).knowledge_revision_id
        == published.knowledge_revision_id
    )
    assert len(live_llm.calls) == calls_before


def test_corrupt_host_content_types_does_not_hide_complete_embedding(
    kb_dir, writer_document, physical_pdf, pdf_model, processing_context, live_llm
):
    from processing_fixtures import finish_recovered_sources

    from ultrakb.application.documents import import_document
    from ultrakb.documents import read_document_source
    from ultrakb.source_catalog import list_sources

    with ZipFile(writer_document) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["[Content_Types].xml"] = b"<Types broken"
    parts["word/embeddings/valid.pdf"] = physical_pdf.read_bytes()
    with ZipFile(writer_document, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    calls_before = len(live_llm.calls)
    parent = import_document(kb_dir, writer_document, context=processing_context)
    assert parent.status == "failed"
    result = finish_recovered_sources(kb_dir, parent.source_id, processing_context)
    children = [s for s in list_sources(kb_dir) if s.source_id != parent.source_id]
    assert len(children) == 1, result
    assert read_document_source(kb_dir, children[0].source_id)["status"] == "completed"
    assert len(live_llm.calls) == calls_before


def test_content_type_default_has_no_phantom_object(
    kb_dir, writer_document, pdf_model, processing_only, processing_context, live_llm
):
    from ultrakb.application.pending import pending_status, process_pending

    raw = writer_document.read_bytes()
    workbook = Workbook()
    workbook.active["A1"] = "Chart evidence"
    data = io.BytesIO()
    workbook.save(data)
    with ZipFile(io.BytesIO(raw)) as original:
        parts = {name: original.read(name) for name in original.namelist()}
    types_xml = parts["[Content_Types].xml"].decode()
    types_xml = types_xml.replace(
        "</Types>",
        '<Default Extension="xlsx" ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet"/></Types>',
    )
    parts["[Content_Types].xml"] = types_xml.encode()
    parts["custom/chart.xlsx"] = data.getvalue()
    with ZipFile(writer_document, "w") as package:
        for name, payload in parts.items():
            package.writestr(name, payload)
    calls_before = len(live_llm.calls)
    processing_only(writer_document)
    process_pending(kb_dir, max_jobs=1, context=processing_context)
    checkpoints = pending_status(kb_dir)["checkpoints"]
    assert all(item["object_key"] for item in checkpoints), checkpoints
    assert processing_context.executor.calls == 0
    assert len(live_llm.calls) == calls_before
