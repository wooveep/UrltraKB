"""GUI opt-in reaches the existing recovery gate without reopening other work."""

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("opt_in", [False, True])
@pytest.mark.parametrize("worksheet", [False, True])
def test_reviewed_failed_summary_option_reaches_worker(kb_dir, monkeypatch, opt_in, worksheet):
    from ultrakb.application.document_actions import DocumentIdentity
    from ultrakb.application.document_batches import DocumentBatchPreview, DocumentRetryTarget
    from ultrakb.desktop.document_batch_actions import requests_for
    from ultrakb.ingest_result import IngestResult
    from ultrakb.runtime.records import UnitIdentity
    from ultrakb.runtime.worker import _execute

    identity = DocumentIdentity("source", "a" * 32)
    targets = tuple(
        DocumentRetryTarget(
            identity,
            "Failed summary" if failed else "Other unfinished work",
            "b" * 32,
            "c" * 32,
            "d" * 32,
            worksheet,
            failed_summary=failed,
        )
        for failed in (True, False)
    )
    preview = DocumentBatchPreview(
        kb_dir, (identity,), (identity,), (), "reviewed", (), "retry", targets
    )
    calls = []

    def receive(root, source_id, **options):
        calls.append((root, source_id, options))
        return IngestResult("retained", "failed", (), message="No generation in adapter test")

    monkeypatch.setattr("ultrakb.application.source_retry.retry_source", receive)
    requests = requests_for(preview, None, retry_failed_response=opt_in)
    assert tuple(request.retry_failed_response for request in requests) == (opt_in, False)
    for index, request in enumerate(requests):
        unit = UnitIdentity.create("e" * 32, index, str(kb_dir), request)
        _execute(request, unit, SimpleNamespace())
    assert [call[2]["retry_failed_response"] for call in calls] == [opt_in, False]
    assert all(call[2]["source_revision_id"] == "b" * 32 for call in calls)
    assert all(call[2]["unit_revision_id"] == "d" * 32 for call in calls)


def test_preview_marks_only_current_failed_summary_targets(kb_dir):
    from publication_recovery_inputs import failed_summary_inputs

    from ultrakb.application.document_actions import DocumentIdentity
    from ultrakb.application.document_batches import preview_document_batch

    fixture = failed_summary_inputs(kb_dir)
    identity = DocumentIdentity("source", fixture.result.source_id)
    preview = preview_document_batch(kb_dir, (identity,), action="retry")
    assert len(preview.retry_targets) == 1
    assert preview.retry_targets[0].failed_summary is True


def test_changed_summary_failure_invalidates_the_review(kb_dir):
    from publication_recovery_inputs import failed_summary_inputs

    from ultrakb.application.document_actions import DocumentIdentity
    from ultrakb.application.document_batches import (
        DocumentBatchChanged,
        preview_document_batch,
        validate_document_batch,
    )
    from ultrakb.compilation_report import CompilationIncomplete
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.unit_publication import record_unit_failure

    fixture = failed_summary_inputs(kb_dir)
    identity = DocumentIdentity("source", fixture.result.source_id)
    preview = preview_document_batch(kb_dir, (identity,), action="retry")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        record_unit_failure(
            kb_dir, fixture.state, "compilation", CompilationIncomplete((), ("concepts",))
        )
    with pytest.raises(DocumentBatchChanged):
        validate_document_batch(preview)


@pytest.mark.asyncio
async def test_third_summary_recovery_keeps_two_accepted_batches_and_original_ledger(kb_dir):
    import json

    from publication_recovery_inputs import failed_summary_inputs, ledger_bytes, retrying
    from test_compile_checkpoints import accepted_summary_input

    from ultrakb.agent.document_summary import (
        SYSTEM_TEMPLATE,
        build_document_summary,
        summary_messages,
    )
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.compile_checkpoints import digest
    from ultrakb.locks import atomic_write_json, atomic_write_text

    fixture = failed_summary_inputs(kb_dir)
    original = "Keep the inlet closed.\n\nInspect before opening.\n\nCheck the interlock."
    source = kb_dir / "wiki/sources/failed.md"
    atomic_write_text(source, original)
    reader = CompileSourceWindows(kb_dir / "wiki", "failed")
    batches = list(reader.iter_document_windows(fits=lambda window: len(window["content"]) <= 26))
    assert len(batches) == 3
    system = {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")}
    requests = [summary_messages(system, "failed", batch, request_format=1) for batch in batches]
    checkpoint = fixture.checkpoint
    for index, (batch, request) in enumerate(zip(batches[:2], requests[:2], strict=True), 1):
        checkpoint.save(f"summary:{index}", digest(request), accepted_summary_input(batch))
    checkpoint.claim_repair("summary:3", digest(requests[2]))
    checkpoint.record_failure("summary:3", digest(requests[2]), "summary_invalid_shape")
    request = fixture.request.model_copy(update={"stage": "compile.document-summary:3"})
    atomic_write_json(fixture.request_path, request.model_dump(mode="json"))
    ledger = ledger_bytes(fixture)
    retained = {path: path.read_bytes() for path in checkpoint.directory.glob("payload-*.json")}
    generated = []

    async def receive(request, stage, originals, sections):
        # Exercise orchestration using explicit local protocol input, with no model executor.
        generated.append(stage)
        assert request == requests[2]
        return accepted_summary_input(batches[2])

    def fits(messages):
        return len(json.loads(messages[1]["content"].split("\n", 1)[1])["content"]) <= 26

    with retrying(fixture), pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        await build_document_summary(
            reader, system_message=system, generate=receive, fits=fits, checkpoints=checkpoint
        )
    assert not generated
    with retrying(fixture, retry_failed_response=True):
        draft = await build_document_summary(
            reader, system_message=system, generate=receive, fits=fits, checkpoints=checkpoint
        )
    assert generated == ["document-summary:3"]
    assert all(sentence in draft.markdown for sentence in original.split("\n\n"))
    assert all(path.read_bytes() == before for path, before in retained.items())
    assert all(path.read_bytes() == before for path, before in ledger.items())
    assert source.read_text() == original


def test_complete_index_is_restored_without_building_or_changing_ledger(kb_dir):
    import json

    from publication_recovery_inputs import failed_summary_inputs, ledger_bytes
    from test_condb_pageindex import indexed_document

    from ultrakb.condb_storage import ConDBPageIndexStorage
    from ultrakb.index_checkpoint import reusable_index
    from ultrakb.index_location import IndexLocation
    from ultrakb.indexer import IndexResult
    from ultrakb.ingest_records import UnitRevision
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.source_catalog import read_admission, read_record, read_source
    from ultrakb.unit_publication import list_source_units

    fixture = failed_summary_inputs(kb_dir)
    source = read_source(kb_dir, fixture.result.source_id)
    admission = read_admission(kb_dir, source)
    [unit] = list_source_units(kb_dir, source.source_id)
    revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
    normalized = SimpleNamespace(fingerprint="{}", source_revision_id=source.target_revision_id)
    working = kb_dir / ".ultrakb/index-fixture"
    location = IndexLocation.package(working / "index")
    with ConDBPageIndexStorage(location) as storage:
        storage.get_or_create_collection("default")
        document = indexed_document(location.inputs_root, identity="retained", name="failed")
        storage.save_document("default", "retained", document)
    tree = {"doc_name": "failed", "structure": document["structure"]}
    index = IndexResult("retained", "Saved navigation", tree)
    ledger = ledger_bytes(fixture)
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        reusable_index(
            kb_dir,
            admission,
            revision,
            normalized,
            kb_dir / admission.revision.original,
            working,
            lambda: index,
        )
    [marker] = (kb_dir / ".ultrakb/index-checkpoints").rglob("checkpoint.json")
    saved = json.loads(marker.read_text())
    retained = {marker.parent / "data" / path: None for path in saved["files"]}
    retained = {path: path.read_bytes() for path in retained}

    def forbidden():
        pytest.fail("A complete retained index must not be regenerated")

    restored = kb_dir / ".ultrakb/restored-index"
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        recovered = reusable_index(
            kb_dir,
            admission,
            revision,
            normalized,
            kb_dir / admission.revision.original,
            restored,
            forbidden,
        )
    assert recovered.doc_id == index.doc_id
    assert (restored / "index/context.sqlite").is_file()
    assert all(path.read_bytes() == value for path, value in retained.items())
    assert all(path.read_bytes() == value for path, value in ledger.items())
