"""Parent task completion retains attachments without creating child work."""

import pytest

from openkb.evidence import ParseStore
from openkb.inputs import prepared_input
from openkb.parsing import parse_document
from openkb.sources import SourceStore
from tests.document_fixtures import write_docx
from tests.docx_attachment_fixtures import attached_docx


def test_parent_completion_and_reimport_do_not_schedule_attachments(
    kb_dir, tmp_path, model_service
):
    from openkb.runtime.requests import ImportFile
    from openkb.runtime.tasks import TaskManager

    child = tmp_path / "instructions.docx"
    write_docx(child, "<w:p><w:r><w:t>Child-only recovery port 9473.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    history = tmp_path / "history"
    manager = TaskManager(history_dir=history)
    try:
        for _ in range(2):
            view = manager.wait(manager.submit(kb_dir, [ImportFile(str(parent))]), timeout=45)
            assert view.state == "completed", view
            assert view.child_task_ids == ()
            assert view.results[0].document.attachments == ()
        assert len(manager.tasks()) == 2
        assert len(SourceStore(kb_dir).list_sources()) == 1
        assert "Child-only recovery port 9473" not in str(model_service)
    finally:
        manager.shutdown(stop=False)
        assert manager.join(10)
    count = len(model_service)
    restored = TaskManager(history_dir=history)
    try:
        assert len(restored.tasks()) == 2
        assert all(
            view.state == "completed" and not view.child_task_ids for view in restored.tasks()
        )
        assert not restored.has_work(kb_dir)
        assert len(model_service) == count
    finally:
        restored.shutdown(stop=True)
        assert restored.join(5)


def test_cli_keeps_nested_attachments_opaque(kb_dir, tmp_path, model_service, monkeypatch, capsys):
    from zipfile import ZipFile

    from openkb.cli_import import import_path

    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "global")
    child = tmp_path / "leaf.docx"
    write_docx(child, "<w:p><w:r><w:t>Nested recovery port 9473.</w:t></w:r></w:p>")
    with ZipFile(child, "a") as archive:
        archive.comment = b"fixture padding" * 300
    middle = attached_docx(tmp_path / "middle.docx", child.read_bytes(), name="leaf.docx")
    parent = attached_docx(tmp_path / "parent.docx", middle.read_bytes(), name="middle.docx")
    assert import_path(kb_dir, str(parent)) == 0
    assert "Attachment task:" not in capsys.readouterr().out
    assert len(SourceStore(kb_dir).list_sources()) == 1
    assert "Nested recovery port 9473" not in str(model_service)


def test_shared_parent_parse_keeps_each_origin_without_registering_children(
    kb_dir, tmp_path, model_service
):
    from openkb.application.documents import import_document
    from openkb.evidence import Evidence

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Recovery instructions.</w:t></w:r></w:p>")
    first = attached_docx(tmp_path / "first.docx", child.read_bytes())
    second = tmp_path / "second.docx"
    second.write_bytes(first.read_bytes())
    results = [import_document(kb_dir, path) for path in (first, second)]
    assert all(result.knowledge_compilation == "completed" for result in results)
    assert results[0].parse_id == results[1].parse_id
    assert results[0].source_id != results[1].source_id
    assert len(SourceStore(kb_dir).list_sources()) == 2
    for result in results:
        parsed = ParseStore(kb_dir).load(result.parse_id)
        reference = Evidence(result.source_id, result.input_version, parsed.id, parsed.blocks[0].id)
        block = ParseStore(kb_dir).read(reference, max_chars=2000)
        assert block.location["attachment_files"][0]["part"] == "word/embeddings/object.bin"
        assert "source_id" not in block.location


def emf_label(*lines):
    """A small MS-EMF drawing whose Unicode text records form the displayed label."""
    import struct

    records = []
    for line in lines:
        encoded = line.encode("utf-16-le")
        size = (76 + len(encoded) + 3) // 4 * 4
        record = bytearray(size)
        struct.pack_into("<II", record, 0, 0x54, size)
        struct.pack_into("<II", record, 44, len(encoded) // 2, 76)
        record[76 : 76 + len(encoded)] = encoded
        records.append(record)
    header = bytearray(88)
    struct.pack_into("<II", header, 0, 1, 88)
    struct.pack_into("<iiiiiiii", header, 8, 0, 0, 100, 50, 0, 0, 2540, 1270)
    struct.pack_into("<I", header, 40, 0x464D4520)
    struct.pack_into("<II", header, 48, 108 + sum(map(len, records)), len(records) + 2)
    return bytes(header) + b"".join(records) + struct.pack("<IIIII", 14, 20, 0, 0, 20)


@pytest.mark.parametrize("damaged", [False, True])
@pytest.mark.parametrize("standalone_icon", [False, True])
def test_body_uses_the_displayed_filename_from_the_object_icon(
    kb_dir, tmp_path, damaged, standalone_icon
):
    from tests.docx_attachment_fixtures import docx_with_parts

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Child instructions.</w:t></w:r></w:p>")
    path = docx_with_parts(
        tmp_path / "parent.docx",
        '<w:p xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<w:r><w:object><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
        '<v:imagedata r:id="icon"/></v:shape>'
        '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
        'Type="Embed" r:id="object"/></w:object></w:r></w:p>'
        + (
            '<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
            '<v:imagedata xmlns:r="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships" '
            'r:id="icon"/></v:shape></w:pict></w:r></w:p>'
            if standalone_icon
            else ""
        ),
        parts={
            "word/embeddings/generated.docx": b"broken container"
            if damaged
            else child.read_bytes(),
            "word/media/icon.emf": emf_label("云平台", "附件.docx"),
        },
        relationships='<Relationship Id="object" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" '
        'Target="embeddings/generated.docx"/>'
        '<Relationship Id="icon" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/icon.emf"/>',
    )
    store = SourceStore(kb_dir)
    with prepared_input(path) as ready:
        source = store.intake(ready)
    parsed = parse_document(kb_dir, source)
    text = "\n".join(store.asset(block.blob).read_text() for block in parsed.blocks)
    assert "云平台附件.docx" in text
    assert "generated.docx" not in text
    assert text.count("![") == int(standalone_icon)
    if damaged:
        assert len(store.list_sources()) == 1
        assert text.count("asset:") == 1 + int(standalone_icon)


def test_document_attachment_alone_is_not_incomplete_image_coverage(
    kb_dir, tmp_path, model_service
):
    from openkb.application.documents import import_document

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Child recovery details.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    result = import_document(kb_dir, parent)
    assert result.knowledge_compilation == "completed"
    assert result.coverage["assets"]
    assert all(
        row["transcription"] == row["understanding"] == "not_required"
        for row in result.coverage["assets"]
    )
    assert result.coverage["status"] == "complete"
