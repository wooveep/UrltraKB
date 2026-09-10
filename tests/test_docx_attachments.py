"""Embedded file content reaches the same immutable evidence gate as the body."""

import hashlib
import struct

import pytest

from openkb.docx_containers import _native_package, unpack_ole
from openkb.evidence import Evidence, ParseStore
from openkb.inputs import prepared_input
from openkb.parsing import parse_document
from openkb.sources import SourceStore
from tests.document_fixtures import write_docx
from tests.docx_attachment_fixtures import (
    attached_docx,
    compound_file,
    docx_with_parts,
    native_package,
)


def test_embedded_docx_is_read_with_container_and_original_positions(kb_dir, tmp_path):
    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Recovery requires port 9473.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    with prepared_input(parent) as ready:
        version = SourceStore(kb_dir).intake(ready)
    parsed = parse_document(kb_dir, version)
    store = ParseStore(kb_dir)
    contents = [
        store.read(Evidence(version.source_id, version.id, parsed.id, b.id), max_chars=2000)
        for b in parsed.blocks
    ]
    child_content = next((item for item in contents if "9473" in item.text), None)
    assert child_content is not None, "Embedded instructions were discarded with the OLE element"
    assert child_content.location["paragraph"] == 1
    attachment = child_content.location["attachment"]
    assert attachment["part"] == "word/embeddings/object.bin"
    assert attachment["position"]["paragraph"] == 1
    assert attachment["blob"] in child_content.assets
    assert store.complete(version, parsed)
    sources = SourceStore(kb_dir).list_sources()
    assert len(sources) == 2
    child_source = next(s for s in sources if s.id != version.id)
    assert child_source.origin.startswith("attachment:" + version.source_id + "/")
    assert child_source.suffix == ".docx"
    assert ParseStore(kb_dir).selected(child_source) is not None
    assert parse_document(kb_dir, version).id == parsed.id
    assert len(SourceStore(kb_dir).list_sources()) == 2


def test_embedded_documents_materialize_as_documents_and_have_parse_status(kb_dir, tmp_path):
    from openkb.application.document_pipeline import _materialize
    from openkb.application.source_history import source_status

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Attachment instructions.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    store = SourceStore(kb_dir)
    with prepared_input(parent) as ready:
        source = store.intake(ready)
    parsed = parse_document(kb_dir, source)
    imported = next(s for s in store.list_sources() if s.origin.startswith("attachment:"))
    workspace = tmp_path / "workspace"
    output = _materialize(workspace, store, source, parsed, "parent")
    text = output.read_text()
    assert "Attachment instructions." in text
    assert f"attachments/{imported.blob}.docx" in text
    assert (output.parent / "attachments" / f"{imported.blob}.docx").read_bytes() == store.original(
        imported
    ).read_bytes()
    status = source_status(kb_dir, imported.source_id)
    assert status["result"]["stage"] == "parsed"
    assert status["result"]["knowledge_compilation"] == "not_started"
    assert status["result"]["parse_id"] == ParseStore(kb_dir).selected(imported).id


def test_ole_package_keeps_counted_script_bytes_without_opening_paths():
    script = b"#!/bin/sh\nprintf 'documented command only'\n"
    name, result = _native_package(native_package("../../run.sh", script))
    assert name == "run.sh" and result == script
    payload = native_package("run.sh", script * 200)
    name, result = unpack_ole(compound_file("\x01Ole10Native", payload))
    assert name == "run.sh" and result == script * 200


def test_only_document_attachments_become_sources(kb_dir, tmp_path):
    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Document-only fact 9473.</w:t></w:r></w:p>")
    files = [
        compound_file("Package", child.read_bytes()),
        compound_file("\x01Ole10Native", native_package("run.sh", b"DO_NOT_IMPORT_SCRIPT\n" * 300)),
        compound_file(
            "\x01Ole10Native", native_package("bundle.zip", b"DO_NOT_OPEN_ARCHIVE\n" * 300)
        ),
    ]
    parts, relationships, body = {}, "", ""
    for index, content in enumerate(files):
        parts[f"word/embeddings/object{index}.bin"] = content
        relationships += (
            f'<Relationship Id="object{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" '
            f'Target="embeddings/object{index}.bin"/>'
        )
        body += (
            "<w:p><w:r><w:object>"
            '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            f'Type="Embed" r:id="object{index}"/></w:object></w:r></w:p>'
        )
    parent = docx_with_parts(
        tmp_path / "mixed.docx", body, parts=parts, relationships=relationships
    )
    store = SourceStore(kb_dir)
    with prepared_input(parent) as ready:
        version = store.intake(ready)
    parsed = parse_document(kb_dir, version)
    text = "\n".join(store.asset(b.blob).read_text() for b in parsed.blocks)
    assert "Document-only fact 9473." in text
    assert "DO_NOT_IMPORT_SCRIPT" not in text and "DO_NOT_OPEN_ARCHIVE" not in text
    assert "Skipped non-document attachment: run.sh" in text
    assert "Skipped non-document attachment: bundle.zip" in text
    assert len(store.list_sources()) == 2
    assert ParseStore(kb_dir).complete(version, parsed)
    for data in (b"DO_NOT_IMPORT_SCRIPT\n" * 300, b"DO_NOT_OPEN_ARCHIVE\n" * 300):
        digest = hashlib.sha256(data).hexdigest()
        assert not store.owned_path(store.root / "blobs" / digest[:2] / digest).exists()


def test_document_attachment_identity_survives_parent_updates(kb_dir, tmp_path):
    child = tmp_path / "child.docx"
    parent = tmp_path / "parent.docx"
    store = SourceStore(kb_dir)
    children = []
    for text in ("Old attachment detail", "Updated attachment detail"):
        write_docx(child, f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>")
        attached_docx(parent, child.read_bytes())
        with prepared_input(parent) as ready:
            version = store.intake(ready)
        parse_document(kb_dir, version)
        children.append(next(s for s in store.list_sources() if s.origin.startswith("attachment:")))
    assert children[0].source_id == children[1].source_id
    assert children[0].id != children[1].id
    assert children[0].revision == 1 and children[1].revision == 2
    assert store.original(children[0]).is_file()


@pytest.mark.parametrize("mutation", ["outer_size", "inner_size", "truncated"])
def test_ole_native_rejects_incomplete_payloads(mutation):
    payload = bytearray(native_package("note.txt", b"Original content"))
    if mutation == "outer_size":
        struct.pack_into("<I", payload, 0, len(payload) + 100)
    elif mutation == "inner_size":
        struct.pack_into("<I", payload, len(payload) - len(b"Original content") - 4, 10000)
    else:
        payload = payload[:10]
    with pytest.raises(ValueError):
        _native_package(bytes(payload))
