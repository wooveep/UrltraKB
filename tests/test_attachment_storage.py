"""Embedded files are retained at source positions without starting content work."""

import hashlib

import pytest

from openkb.evidence import Evidence, ParseStore
from openkb.inputs import prepared_input
from openkb.parsing import parse_document
from openkb.sources import SourceStore
from tests.docx_attachment_fixtures import attached_docx


@pytest.mark.parametrize(
    "name", ["notes.txt", "run.sh", "bundle.zip", "broken.pdf", "embedded.docx"]
)
def test_embedded_files_are_saved_without_analysis_or_child_sources(kb_dir, tmp_path, name):
    content = b"Attachment-only bytes; never parent evidence.\x00\xff" * 200
    parent = attached_docx(tmp_path / "parent.docx", content, name=name)
    store = SourceStore(kb_dir)
    with prepared_input(parent) as ready:
        source = store.intake(ready)
    parsed = parse_document(kb_dir, source)
    reference = Evidence(source.source_id, source.id, parsed.id, parsed.blocks[0].id)
    saved = ParseStore(kb_dir).read(reference, max_chars=2000)
    digest = hashlib.sha256(content).hexdigest()
    assert store.asset(digest).read_bytes() == content
    assert saved.location["attachment_files"] == [
        {"part": "word/embeddings/object.bin", "name": name, "blob": digest, "parseable": False}
    ]
    assert saved.location["paragraph"] == 1
    assert saved.assets == (digest,)
    assert f"[{name}](asset:{digest})" in saved.text
    assert "Attachment-only" not in saved.text
    assert ParseStore(kb_dir).complete(source, parsed)
    assert store.list_sources() == (source,)
    assert parse_document(kb_dir, source).id == parsed.id


@pytest.mark.parametrize("accept_candidate", [False, True])
def test_continuing_legacy_parse_does_not_register_an_attachment_source(
    kb_dir, tmp_path, model_service, accept_candidate
):
    from openkb.application.source_actions import continue_source
    from openkb.evidence import BlockDraft

    content = b"Attachment-only content." * 200
    parent = attached_docx(tmp_path / "parent.docx", content, name="child.txt")
    store = SourceStore(kb_dir)
    with prepared_input(parent) as ready:
        source = store.intake(ready)
    parsed = parse_document(kb_dir, source)
    drafts = []
    for block in parsed.blocks:
        location = dict(block.location)
        location["attachment_files"] = [
            {**item, "parseable": True} for item in location["attachment_files"]
        ]
        drafts.append(
            BlockDraft(store.asset(block.blob).read_text(), block.kind, location, block.assets)
        )
    legacy = ParseStore(kb_dir).save(source, {"legacy": "stored-attachment"}, drafts)
    ParseStore(kb_dir).select(source, legacy)
    if accept_candidate:
        (kb_dir / "wiki/index.md").write_text("# Human index\nKeep this context.\n")
    result = continue_source(kb_dir, source.source_id, version_id=source.id)
    if accept_candidate:
        from openkb.application.source_actions import review_source_proposal

        assert result.reason == "needs_acceptance"
        review = review_source_proposal(kb_dir, result.resume)
        calls = len(model_service)
        result = continue_source(
            kb_dir,
            source.source_id,
            version_id=source.id,
            proposal_id=result.resume,
            accept_pages=review["protected"],
        )
        assert len(model_service) == calls
    assert result.knowledge_compilation == "completed", result
    assert result.parse_id == legacy.id
    assert result.attachments == ()
    assert store.list_sources() == (source,)
    assert all(b"Attachment-only content" not in str(body).encode() for body in model_service)


def test_unextractable_file_retains_raw_object_and_location(kb_dir, tmp_path):
    from zipfile import ZipFile

    parent = attached_docx(tmp_path / "parent.docx", b"Unrecognized package data")
    with ZipFile(parent) as package:
        raw = package.read("word/embeddings/object.bin")
    store = SourceStore(kb_dir)
    with prepared_input(parent) as ready:
        source = store.intake(ready)
    parsed = parse_document(kb_dir, source)
    block = parsed.blocks[0]
    item = block.location["attachment_files"][0]
    assert store.asset(item["blob"]).read_bytes() == raw
    assert item["extraction"] == "raw_object"
    assert item["reason"] == "docx_ole_package_unsupported"
    assert item["blob"] in block.assets
    assert block.location["paragraph"] == 1
    assert ParseStore(kb_dir).complete(source, parsed)


def test_old_automatic_attachment_queue_is_stopped_without_erasing_history(kb_dir, tmp_path):
    from dataclasses import asdict

    from openkb.locks import atomic_write_json
    from openkb.runtime.records import TaskView, UnitIdentity
    from openkb.runtime.requests import ImportAttachment
    from openkb.runtime.tasks import TaskManager

    history = tmp_path / "history"
    task_id, parent_id = "a" * 32, "b" * 32
    request = ImportAttachment("c" * 32, "d" * 64, "e" * 64, "word/child.docx", "child.docx")
    identity = UnitIdentity.create(task_id, 0, str(kb_dir), request)
    view = TaskView(
        task_id,
        str(kb_dir),
        "ImportAttachment",
        "queued",
        "queued",
        1,
        (),
        False,
        True,
        parent_task_id=parent_id,
    )
    atomic_write_json(
        history / f"{task_id}.json", {"view": view.summary(), "identities": [asdict(identity)]}
    )
    manager = TaskManager(history_dir=history)
    try:
        restored = manager.get(task_id)
        assert restored.state == "stopped"
        assert restored.stage == "attachment-stored"
        assert restored.parent_task_id == parent_id
        assert not manager.has_work(kb_dir)
        assert (history / f"{task_id}.json").is_file()
    finally:
        manager.shutdown(stop=True)
        assert manager.join(5)


def test_attachment_positions_and_bytes_survive_cleanup_and_new_parse_selection(kb_dir, tmp_path):
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup
    from openkb.evidence import BlockDraft
    from tests.docx_attachment_fixtures import compound_file, docx_with_parts, native_package

    contents = [b"First opaque payload.\n" * 300, b"Different opaque payload.\n" * 300]
    parts = {
        f"word/embeddings/{i}.bin": compound_file(
            "\x01Ole10Native", native_package("same.sh", data)
        )
        for i, data in enumerate(contents)
    }
    relationships = "".join(
        f'<Relationship Id="file{i}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/oleObject" Target="embeddings/{i}.bin"/>'
        for i in range(2)
    )

    def object_xml(i):
        return (
            '<w:r><w:object><o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            f'Type="Embed" r:id="file{i}"/></w:object></w:r>'
        )

    parent = docx_with_parts(
        tmp_path / "positions.docx",
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Files</w:t></w:r></w:p>'
        + "<w:p>"
        + object_xml(0)
        + object_xml(1)
        + "</w:p>"
        + "<w:tbl><w:tr><w:tc><w:p>"
        + object_xml(0)
        + "</w:p></w:tc></w:tr></w:tbl>",
        parts=parts,
        relationships=relationships,
    )
    store, parses = SourceStore(kb_dir), ParseStore(kb_dir)
    with prepared_input(parent) as ready:
        source = store.intake(ready)
    parsed = parse_document(kb_dir, source)
    blocks = [block for block in parsed.blocks if "attachment_files" in block.location]
    first, second = blocks
    refs = first.location["attachment_files"]
    assert [item["name"] for item in refs] == ["same.sh", "same.sh"]
    assert refs[0]["blob"] != refs[1]["blob"]
    assert second.location["attachment_files"] == [refs[0]]
    assert first.location["paragraph"] != second.location["paragraph"]
    assert first.location["headings"] == second.location["headings"] == ["Files"]
    assert second.location["table"] == 1
    references = [Evidence(source.source_id, source.id, parsed.id, b.id) for b in blocks]
    for item, data in zip(refs, contents):
        assert store.asset(item["blob"]).read_bytes() == data
    # Current parse selection can move; saved evidence still binds the original parse.
    newer = parses.save(
        source,
        {"fixture": "new-interpretation"},
        [BlockDraft("New view", "paragraph", {"kind": "docx", "paragraph": 1})],
    )
    parses.select(source, newer)
    (kb_dir / "wiki/concepts/retained.md").write_text(
        f"Retain {source.id} / {parsed.id} / {first.id}"
    )
    preview = preview_history_cleanup(kb_dir)
    cleanup_history(kb_dir, preview.id)
    for reference, expected in zip(references, blocks):
        actual = parses.read(reference, max_chars=3000)
        assert actual.location == expected.location
        for item in actual.location["attachment_files"]:
            assert store.asset(item["blob"]).is_file()


@pytest.mark.parametrize("parent_gap", [False, True])
def test_legacy_expanded_attachment_text_never_enters_parent_model_input(
    kb_dir, tmp_path, model_service, parent_gap
):
    import yaml

    from openkb.application.source_actions import continue_source
    from openkb.evidence import BlockDraft
    from tests.document_fixtures import write_docx

    path = tmp_path / "legacy.docx"
    write_docx(path, "<w:p><w:r><w:t>Parent text.</w:t></w:r></w:p>")
    store, parses = SourceStore(kb_dir), ParseStore(kb_dir)
    with prepared_input(path) as ready:
        source = store.intake(ready)
    blob = store.put_bytes(b"LEGACY_CHILD_SECRET")
    image = store.put_bytes(b"Retained child image bytes")
    child_location = {
        "kind": "docx",
        "paragraph": 1,
        "attachment": {
            "part": "word/embeddings/child.txt",
            "name": "child.txt",
            "blob": blob,
            "position": {"kind": "text", "line": 1},
        },
    }
    parsed = parses.save(
        source,
        {"docx": "legacy-inline-attachments"},
        [
            BlockDraft("Parent instructions.", "paragraph", {"kind": "docx", "paragraph": 1}),
            BlockDraft("# LEGACY_CHILD_SECRET heading", "heading", child_location, (blob,)),
            BlockDraft("LEGACY_CHILD_SECRET body", "paragraph", child_location, (blob,)),
            BlockDraft(
                f"![LEGACY_CHILD_SECRET](asset:{image})", "image", child_location, (blob, image)
            ),
            BlockDraft("Parent conclusion.", "paragraph", {"kind": "docx", "paragraph": 2}),
        ],
        quality=[
            {
                "status": "needs_review",
                "reason": "docx_image_ocr_notice:unavailable",
                "location": child_location,
            }
        ]
        + (
            [{"status": "needs_review", "reason": "docx_unreadable_parent_object"}]
            if parent_gap
            else []
        ),
    )
    parses.select(source, parsed)
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["navigation"] = {"enabled": True, "summaries": True}
    config_path.write_text(yaml.safe_dump(config))
    result = continue_source(kb_dir, source.source_id, version_id=source.id)
    assert result.knowledge_compilation == "completed", result
    assert "LEGACY_CHILD_SECRET" not in str(model_service)
    assert "Parent instructions" in str(model_service)
    assert result.coverage["status"] == ("partial" if parent_gap else "complete")
    if parent_gap:
        assert "dependencies" in str(model_service)
    assert all(
        row["status"] == "stored" and row["reason"] == "attachment_stored_only"
        for row in result.coverage["ranges"]
        if "attachment" in row["location"]
    )
    old = parses.read(
        Evidence(source.source_id, source.id, parsed.id, parsed.blocks[2].id), max_chars=100
    )
    assert old.text == "LEGACY_CHILD_SECRET body"
    assert parses.load(parsed.id) == parsed
    assert store.list_sources() == (source,)


def test_legacy_attachment_ranges_do_not_consume_parent_budget_or_overlap_old_units(
    kb_dir, tmp_path, monkeypatch
):
    from openkb.compilation_report import CompileReport
    from openkb.evidence import BlockDraft, complete_read_bound
    from openkb.navigation_evidence import evidence_descriptor, read_evidence_group
    from openkb.source_coverage import source_coverage

    path = tmp_path / "legacy.md"
    path.write_text("Parent text")
    store, parses = SourceStore(kb_dir), ParseStore(kb_dir)
    with prepared_input(path) as ready:
        source = store.intake(ready)
    blob = store.put_bytes(b"Original child bytes")
    parsed = parses.save(
        source,
        {"fixture": "legacy-child"},
        [
            BlockDraft("Parent text", "paragraph", {"kind": "text", "line": 1}),
            BlockDraft(
                "Child text" * 1000,
                "paragraph",
                {
                    "kind": "docx",
                    "paragraph": 1,
                    "attachment": {
                        "part": "word/child.txt",
                        "name": "child.txt",
                        "blob": blob,
                        "position": {"kind": "text", "line": 1},
                    },
                },
                (blob,),
            ),
        ],
    )
    admitted = []
    monkeypatch.setattr(
        "openkb.resource_budget.check_memory", lambda amount, **_: admitted.append(amount)
    )
    group = read_evidence_group(kb_dir, source, parsed, evidence_descriptor(source, parsed, 0, 2))
    assert [block["text"] for block in group["blocks"]] == ["Parent text"]
    assert admitted == [complete_read_bound(parsed.blocks[0]) * 12]
    report = CompileReport(
        source_units={
            block.id: {
                "reference": {"block_id": block.id, "start": 0, "end": block.chars},
                "facts": [],
                "empty_reason": "no_knowledge_content",
            }
            for block in parsed.blocks
        }
    )
    coverage = source_coverage(source, parsed, report, published=True)
    assert coverage["status"] == "complete"
    assert [row["status"] for row in coverage["ranges"]] == ["no_facts", "stored"]
