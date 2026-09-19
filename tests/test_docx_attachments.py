"""Embedded files stay stored references at their immutable parent positions."""

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


def test_embedded_docx_keeps_bytes_and_parent_position_without_analysis(kb_dir, tmp_path):
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
    assert not any("Recovery requires port 9473." in item.text for item in contents)
    reference = next(item for item in contents if "object.docx" in item.text)
    assert reference.location["paragraph"] == 1
    attachment = reference.location["attachment_files"][0]
    assert attachment["part"] == "word/embeddings/object.bin"
    assert attachment["parseable"] is False
    assert attachment["blob"] in reference.assets
    assert store.complete(version, parsed)
    sources = SourceStore(kb_dir).list_sources()
    assert sources == (version,)
    assert SourceStore(kb_dir).asset(attachment["blob"]).read_bytes() == child.read_bytes().ljust(
        4096, b"\0"
    )
    assert parse_document(kb_dir, version).id == parsed.id
    assert len(SourceStore(kb_dir).list_sources()) == 1


def test_embedded_files_materialize_without_analysis(kb_dir, tmp_path):
    from openkb.application.document_pipeline import _materialize

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Attachment instructions.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    store = SourceStore(kb_dir)
    with prepared_input(parent) as ready:
        source = store.intake(ready)
    parsed = parse_document(kb_dir, source)
    blob = parsed.blocks[0].location["attachment_files"][0]["blob"]
    output = _materialize(tmp_path / "workspace", store, source, parsed, "parent")
    assert "Attachment instructions." not in output.read_text()
    assert f"attachments/{blob}.docx" in output.read_text()
    assert (
        output.parent / "attachments" / f"{blob}.docx"
    ).read_bytes() == child.read_bytes().ljust(4096, b"\0")
    assert store.list_sources() == (source,)


def test_ole_package_keeps_counted_script_bytes_without_opening_paths():
    script = b"#!/bin/sh\nprintf 'documented command only'\n"
    name, result = _native_package(native_package("../../run.sh", script))
    assert name == "run.sh" and result == script
    payload = native_package("run.sh", script * 200)
    name, result = unpack_ole(compound_file("\x01Ole10Native", payload))
    assert name == "run.sh" and result == script * 200


def test_all_attachment_types_are_stored_without_becoming_sources(kb_dir, tmp_path):
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
    assert "Document-only fact 9473." not in text
    assert "DO_NOT_IMPORT_SCRIPT" not in text and "DO_NOT_OPEN_ARCHIVE" not in text
    assert "run.sh" in text and "bundle.zip" in text
    assert "Skipped non-document attachment:" not in text
    assert len(store.list_sources()) == 1
    assert ParseStore(kb_dir).complete(version, parsed)
    for data in (b"DO_NOT_IMPORT_SCRIPT\n" * 300, b"DO_NOT_OPEN_ARCHIVE\n" * 300):
        digest = hashlib.sha256(data).hexdigest()
        assert store.asset(digest).read_bytes() == data


def test_attachment_references_survive_parent_updates(kb_dir, tmp_path):
    child = tmp_path / "child.docx"
    parent = tmp_path / "parent.docx"
    store = SourceStore(kb_dir)
    references, contents = [], []
    for text in ("Old attachment detail", "Updated attachment detail"):
        write_docx(child, f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>")
        contents.append(child.read_bytes())
        attached_docx(parent, contents[-1])
        with prepared_input(parent) as ready:
            version = store.intake(ready)
        parsed = parse_document(kb_dir, version)
        references.append(Evidence(version.source_id, version.id, parsed.id, parsed.blocks[0].id))
    assert references[0].source_id == references[1].source_id
    assert references[0].version_id != references[1].version_id
    for reference, content in zip(references, contents):
        block = ParseStore(kb_dir).read(reference, max_chars=2000)
        item = block.location["attachment_files"][0]
        assert store.asset(item["blob"]).read_bytes() == content.ljust(4096, b"\0")
    assert len(store.list_sources()) == 1


def test_document_attachments_compile_and_publish_as_downloadable_sources(
    kb_dir, tmp_path, model_service
):
    from openkb.application.documents import import_document

    child = tmp_path / "instructions.docx"
    write_docx(child, "<w:p><w:r><w:t>Recovery requires port 9473.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "manual.docx", child.read_bytes())
    result = import_document(kb_dir, parent)
    assert result.knowledge_compilation == "completed", result
    assert list((kb_dir / "wiki/sources/attachments").glob("*.docx"))


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
