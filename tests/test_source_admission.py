"""Source identity and durable admission are independent of byte deduplication."""

import json
from types import SimpleNamespace

import pytest

from openkb.inputs import prepared_input


@pytest.fixture
def pdf_source(kb_dir):
    import pymupdf

    original = kb_dir / "notes.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "A frozen source fact.")
        pdf.save(original)
    return original


@pytest.fixture
def fixed_model(monkeypatch):
    replies = iter(
        [
            {"description": "Notes", "content": "# Notes\n\nA source fact."},
            {"create": [], "update": [], "related": []},
        ]
    )

    def completion(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(replies))))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr("litellm.completion", completion)


def test_equal_bytes_keep_distinct_sources(kb_dir, tmp_path):
    from openkb.source_catalog import admit_source_revision, list_sources, read_source_revision

    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    first.write_bytes(b"same frozen original")
    second.write_bytes(first.read_bytes())
    with prepared_input(first) as ready:
        a = admit_source_revision(kb_dir, ready)
    with prepared_input(second) as ready:
        b = admit_source_revision(kb_dir, ready)

    assert a.source.source_id != b.source.source_id
    assert {source.name for source in list_sources(kb_dir)} == {"first.pdf", "second.pdf"}
    assert (
        read_source_revision(kb_dir, a.revision.source_revision_id).original == b.revision.original
    )
    assert (kb_dir / a.revision.original).read_bytes() == b"same frozen original"


def test_repeating_admission_reuses_the_frozen_target(kb_dir):
    from openkb.source_catalog import admit_source_revision

    original = kb_dir / "notes.pdf"
    original.write_bytes(b"frozen original")
    with prepared_input(original) as ready:
        first = admit_source_revision(kb_dir, ready)
        replay = admit_source_revision(kb_dir, ready)
    assert replay == first


def test_body_failure_keeps_the_admitted_original(kb_dir, pdf_source, monkeypatch):
    from openkb.application.documents import import_document
    from openkb.source_catalog import read_source_revision

    def unavailable(**kwargs):
        raise ConnectionError("provider unavailable")

    monkeypatch.setattr("litellm.completion", unavailable)
    result = import_document(kb_dir, pdf_source)
    assert result.status == "failed"
    revision = read_source_revision(kb_dir, result.source_revision_id)
    assert (kb_dir / revision.original).read_bytes() == pdf_source.read_bytes()
    assert result.units[0].error_type == "ConnectionError"
    assert "provider unavailable" in result.message


def test_lost_notification_does_not_repeat_committed_model_work(kb_dir, pdf_source, fixed_model):
    from openkb.application.documents import import_document

    def lost_notification(event):
        if event["stage"] == "committed":
            raise BrokenPipeError("observer disappeared")

    first = import_document(kb_dir, pdf_source, on_event=lost_notification)
    replay = import_document(kb_dir, pdf_source)
    assert first.status == "added"
    assert replay.status == "skipped"
    assert replay.units[0].knowledge_revision_id == first.units[0].knowledge_revision_id


def test_publish_commit_failure_restores_knowledge(kb_dir, pdf_source, fixed_model, monkeypatch):
    from openkb.application.documents import import_document
    from openkb.mutation import MutationSnapshot
    from openkb.source_catalog import read_source_revision
    from openkb.unit_publication import read_head

    commit = MutationSnapshot.mark_committed

    def fail_publication(snapshot):
        if snapshot.operation == "publish-unit-revision":
            raise OSError("commit marker unavailable")
        commit(snapshot)

    monkeypatch.setattr(MutationSnapshot, "mark_committed", fail_publication)
    result = import_document(kb_dir, pdf_source)
    assert result.status == "failed"
    assert read_head(kb_dir).knowledge_revision_id is None
    assert not (kb_dir / "wiki/summaries/notes.md").exists()
    assert (kb_dir / read_source_revision(kb_dir, result.source_revision_id).original).exists()


def test_unknown_legacy_baseline_is_preserved(kb_dir, pdf_source, fixed_model):
    from openkb.application.documents import import_document

    page = kb_dir / "wiki/summaries/notes.md"
    page.write_text("Keep the existing human explanation.")
    result = import_document(kb_dir, pdf_source)
    assert result.status == "blocked"
    assert page.read_text() == "Keep the existing human explanation."
    assert result.units[0].proposal_id


def test_published_source_is_listed_and_read_by_its_identity(kb_dir, pdf_source, fixed_model):
    from openkb.application.documents import import_document
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.documents import read_document_source

    result = import_document(kb_dir, pdf_source)
    documents = get_kb_list(kb_dir)["documents"]
    assert documents[0]["source_id"] == result.source_id
    source = read_document_source(kb_dir, result.source_id)
    assert source["source_revision_id"] == result.source_revision_id
    assert "A frozen source fact." in source["content"]


def test_upload_cleanup_keeps_admitted_input_with_pending_discovery(kb_dir, pdf_source):
    from openkb.application.uploads import published_input
    from openkb.source_catalog import admit_source_revision

    with published_input(kb_dir, pdf_source) as uploaded:
        with prepared_input(uploaded.path) as ready:
            admit_source_revision(kb_dir, ready)
        uploaded.discard_if_unregistered()
        assert uploaded.path.read_bytes() == pdf_source.read_bytes()


def test_unknown_catalog_capability_blocks_a_writer(kb_dir, pdf_source):
    from openkb.application.documents import import_document

    catalog = kb_dir / ".openkb/catalog"
    catalog.mkdir()
    (catalog / "schema.json").write_text(
        json.dumps({"schema_version": 1, "required_capabilities": ["future-inputs"]})
    )
    with pytest.raises(ValueError, match="capabilit"):
        import_document(kb_dir, pdf_source)


def test_failed_new_target_keeps_the_exact_previous_success(
    kb_dir, pdf_source, fixed_model, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    first = import_document(kb_dir, pdf_source)
    with pdf_source.open("ab") as stream:
        stream.write(b"\n% changed source revision\n")

    def unavailable(**kwargs):
        raise ConnectionError("provider unavailable")

    monkeypatch.setattr("litellm.completion", unavailable)
    failed = import_document(kb_dir, pdf_source)
    actual = read_document_source(kb_dir, first.source_id)
    assert failed.status == "failed" and failed.source_revision_id != first.source_revision_id
    assert failed.units[0].successful_source_revision_id == first.source_revision_id
    assert actual["source_revision_id"] == first.source_revision_id
    assert actual["target_source_revision_id"] == failed.source_revision_id
    assert "provider unavailable" in actual["message"]


def test_cancellation_before_admission_commit_keeps_no_partial_source(kb_dir, pdf_source):
    from openkb.locks import LockCancelled
    from openkb.source_catalog import admit_source_revision, list_sources

    calls = 0

    def cancelled_before_commit():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LockCancelled("cancelled before admission commit")

    with prepared_input(pdf_source) as ready, pytest.raises(LockCancelled):
        admit_source_revision(kb_dir, ready, check_stop=cancelled_before_commit)
    assert list_sources(kb_dir) == ()


@pytest.mark.parametrize(
    "field,value",
    [
        ("normalized_source", "../../../../../config.yaml"),
        ("unit_revision_id", "0" * 32),
    ],
)
def test_source_reader_rejects_corrupt_snapshot_links(
    kb_dir, pdf_source, fixed_model, field, value
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    result = import_document(kb_dir, pdf_source)
    revision_id = result.units[0].knowledge_revision_id
    manifest = kb_dir / f".openkb/knowledge/legacy/revisions/{revision_id}/manifest.json"
    data = json.loads(manifest.read_text())
    data[field] = value
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_document_source(kb_dir, result.source_id)


def test_cancelled_body_records_stopped_and_can_be_explicitly_retried(
    kb_dir, pdf_source, fixed_model
):
    from openkb.application.documents import import_document
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.locks import LockCancelled

    def cancel(event):
        if event["stage"] == "converting":
            raise LockCancelled("User cancelled the import")

    with pytest.raises(LockCancelled):
        import_document(kb_dir, pdf_source, on_event=cancel)
    assert get_kb_list(kb_dir)["documents"][0]["status"] == "stopped"
    assert import_document(kb_dir, pdf_source).status == "added"


def test_earlier_success_is_readable_after_a_later_publication(
    kb_dir, pdf_source, fixed_model, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    first = import_document(kb_dir, pdf_source)
    with pdf_source.open("ab") as stream:
        stream.write(b"\n% next revision\n")
    replies = iter(
        [
            {"description": "New notes", "content": "# New notes\n\nNew explanation."},
            {"create": [], "update": [], "related": []},
        ]
    )
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(replies))))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        ),
    )
    assert import_document(kb_dir, pdf_source).status == "added"
    old = read_document_source(kb_dir, first.source_id, source_revision_id=first.source_revision_id)
    assert old["knowledge_revision_id"] == first.units[0].knowledge_revision_id
    assert old["source_revision_id"] == first.source_revision_id
