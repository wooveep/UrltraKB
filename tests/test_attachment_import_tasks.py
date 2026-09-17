"""Embedded document imports are separate work; parent text retains file names."""

import pytest

from openkb.evidence import ParseStore
from openkb.inputs import prepared_input
from openkb.parsing import parse_document
from openkb.sources import SourceStore
from tests.document_fixtures import write_docx
from tests.docx_attachment_fixtures import attached_docx


def test_parent_body_retains_attachment_name_without_child_contents(kb_dir, tmp_path):
    child = tmp_path / "instructions.docx"
    write_docx(child, "<w:p><w:r><w:t>Child-only recovery port 9473.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    store = SourceStore(kb_dir)
    with prepared_input(parent) as ready:
        source = store.intake(ready)
    parsed = parse_document(kb_dir, source)
    text = "\n".join(store.asset(block.blob).read_text() for block in parsed.blocks)
    assert "object.docx" in text
    assert "Child-only recovery port 9473." not in text
    assert "Embedded attachment:" not in text
    child_source = next(row for row in store.list_sources() if row.id != source.id)
    child_parse = ParseStore(kb_dir).selected(child_source)
    assert child_parse is not None
    assert "Child-only recovery port 9473." in "\n".join(
        store.asset(block.blob).read_text() for block in child_parse.blocks
    )


def test_document_attachment_gets_an_independent_import_task(kb_dir, tmp_path, model_service):
    from openkb.application.source_history import source_status
    from openkb.runtime.requests import ImportFile
    from openkb.runtime.tasks import TaskManager

    child = tmp_path / "instructions.docx"
    write_docx(child, "<w:p><w:r><w:t>Child-only recovery port 9473.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        parent_id = manager.submit(kb_dir, [ImportFile(str(parent))])
        parent_view = manager.wait(parent_id, timeout=60)
        assert len(parent_view.child_task_ids) == 1
        child_view = manager.wait(parent_view.child_task_ids[0], timeout=60)
        assert child_view.parent_task_id == parent_id
        assert child_view.operation == "ImportAttachment"
        assert child_view.state == parent_view.state == "completed"
        result = child_view.results[0].document
        assert result is not None and result.source_id != parent_view.results[0].document.source_id
        assert (
            source_status(kb_dir, result.source_id)["result"]["knowledge_compilation"]
            == "completed"
        )
        repeat = manager.submit(kb_dir, [ImportFile(str(parent))])
        manager.wait(repeat, timeout=60)
        assert len(manager.tasks()) == 3, (
            "Repeating the parent must not create another child import"
        )
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


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
def test_body_uses_the_displayed_filename_from_the_object_icon(kb_dir, tmp_path, damaged):
    from tests.docx_attachment_fixtures import docx_with_parts

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Child instructions.</w:t></w:r></w:p>")
    path = docx_with_parts(
        tmp_path / "parent.docx",
        '<w:p xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<w:r><w:object><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
        '<v:imagedata r:id="icon"/></v:shape>'
        '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
        'Type="Embed" r:id="object"/></w:object></w:r></w:p>',
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
    assert "![" not in text
    if damaged:
        assert len(store.list_sources()) == 1
        assert "asset:" not in text


def test_stopping_parent_stops_its_running_attachment(kb_dir, tmp_path, model_service):
    import json
    import threading

    from http_model_fixture import evidence_response

    from openkb.runtime.requests import ImportFile
    from openkb.runtime.tasks import TaskManager

    child_started, release_child = threading.Event(), threading.Event()

    def response(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload.get("stage") == "facts" and "Child-only" in json.dumps(payload):
            child_started.set()
            release_child.wait(30)
        return evidence_response(payload)

    model_service.respond = response
    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Child-only recovery instructions.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        parent_id = manager.submit(kb_dir, [ImportFile(str(parent))])
        parent_view = manager.wait(parent_id, timeout=30)
        assert child_started.wait(20)
        assert parent_view.state == "completed"
        manager.stop(parent_id)
        child_view = manager.wait(parent_view.child_task_ids[0], timeout=20)
        assert child_view.state == "stopped" and child_view.processes_reaped
        assert manager.get(parent_id).state == "completed"
    finally:
        release_child.set()
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_graceful_drain_finishes_children_and_history_does_not_restart_them(
    kb_dir, tmp_path, model_service
):
    from openkb.runtime.requests import ImportFile
    from openkb.runtime.tasks import TaskManager

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Child recovery instructions.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    history = tmp_path / "history"
    manager = TaskManager(history_dir=history)
    try:
        parent_id = manager.submit(kb_dir, [ImportFile(str(parent))])
        manager.shutdown(stop=False)
        assert manager.join(45)
        child_id = manager.get(parent_id).child_task_ids[0]
        assert manager.get(child_id).state == "completed"
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
    count = len(model_service)
    restored = TaskManager(history_dir=history)
    try:
        assert restored.get(parent_id).child_task_ids == (child_id,)
        assert restored.get(child_id).parent_task_id == parent_id
        assert not restored.has_work(kb_dir)
        assert len(model_service) == count
    finally:
        restored.shutdown(stop=True)
        assert restored.join(5)


def test_cli_waits_for_nested_attachment_imports(
    kb_dir, tmp_path, model_service, monkeypatch, capsys
):
    from zipfile import ZipFile

    from openkb.cli_import import import_path

    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "global")
    child = tmp_path / "leaf.docx"
    write_docx(child, "<w:p><w:r><w:t>Nested recovery port 9473.</w:t></w:r></w:p>")
    # The fixture CFB writer uses regular streams, whose minimum size is 4096.
    with ZipFile(child, "a") as archive:
        archive.comment = b"fixture padding" * 300
    middle = attached_docx(tmp_path / "middle.docx", child.read_bytes(), name="leaf.docx")
    parent = attached_docx(tmp_path / "parent.docx", middle.read_bytes(), name="middle.docx")
    assert import_path(kb_dir, str(parent)) == 0
    output = capsys.readouterr().out
    assert output.count("Attachment task:") == 2
    assert "leaf.docx · completed" in output
    assert "middle.docx · completed" in output
    assert len(SourceStore(kb_dir).list_sources()) == 3


def test_shared_parent_parse_binds_child_imports_to_each_origin(kb_dir, tmp_path, model_service):
    from openkb.runtime.requests import ImportFile
    from openkb.runtime.tasks import TaskManager

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Recovery instructions.</w:t></w:r></w:p>")
    first = attached_docx(tmp_path / "first.docx", child.read_bytes())
    second = tmp_path / "second.docx"
    second.write_bytes(first.read_bytes())
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        parents, children = [], []
        for path in (first, second):
            view = manager.wait(manager.submit(kb_dir, [ImportFile(str(path))]), timeout=30)
            parents.append(view.results[0].document)
            imported = manager.wait(view.child_task_ids[0], timeout=30)
            assert imported.state == "completed"
            children.append(imported.results[0].document)
        assert parents[0].parse_id == parents[1].parse_id
        assert children[0].source_id != children[1].source_id
        assert all(
            SourceStore(kb_dir)
            .version(child.input_version)
            .origin.startswith("attachment:" + parent.source_id + "/")
            for child, parent in zip(children, parents, strict=True)
        )
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
