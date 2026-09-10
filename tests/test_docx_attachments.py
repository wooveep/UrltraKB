"""Embedded file content reaches the same immutable evidence gate as the body."""

import io
import struct
from zipfile import ZipFile

import pytest

from openkb.docx_containers import _native_package, unpack_ole
from openkb.evidence import Evidence, ParseStore
from openkb.inputs import prepared_input
from openkb.parsing import parse_document
from openkb.sources import SourceStore
from tests.document_fixtures import write_docx
from tests.docx_attachment_fixtures import attached_docx, compound_file, native_package


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
    assert parse_document(kb_dir, version).id == parsed.id


def test_ole_package_keeps_counted_script_bytes_without_opening_paths():
    script = b"#!/bin/sh\nprintf 'documented command only'\n"
    name, result = _native_package(native_package("../../run.sh", script))
    assert name == "run.sh" and result == script
    payload = native_package("run.sh", script * 200)
    name, result = unpack_ole(compound_file("\x01Ole10Native", payload))
    assert name == "run.sh" and result == script * 200


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
