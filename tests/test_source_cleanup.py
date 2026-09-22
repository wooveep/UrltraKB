"""Explicit history cleanup protects readable citations and shared content."""

import pytest

from openkb.application.documents import import_document
from openkb.application.source_actions import read_source_evidence
from openkb.evidence import Evidence, ParseStore
from openkb.sources import SourceStore


def test_pending_proposal_shared_with_authorship_keeps_its_original_citations(kb_dir, tmp_path):
    from openkb.application.documents import DocumentResult
    from openkb.application.source_cleanup import preview_history_cleanup
    from openkb.application.source_history import record_source_result
    from openkb.inputs import prepared_input
    from openkb.knowledge_commit import KnowledgeWorkspace
    from openkb.locks import atomic_write_text
    from tests.test_knowledge_proposals import inputs, publish_proposal

    sources = []
    for name in ("target", "author", "other"):
        folder = tmp_path / name
        folder.mkdir()
        sources.append(inputs(kb_dir, folder))
    (old, parsed), (author, author_parse), (other, other_parse) = sources
    cited = f"[Old](sources/snapshots/{old.id}-{parsed.id}.md#block-{parsed.blocks[0].id})"
    with KnowledgeWorkspace(kb_dir, author, author_parse, {"model": "test"}) as workspace:
        atomic_write_text(workspace.path / "wiki/concepts/owned.md", cited)
        authored = workspace.proposal()
    assert publish_proposal(kb_dir, authored.id).status == "completed"
    atomic_write_text(kb_dir / "wiki/concepts/owned.md", "Manual text without the old citation.")
    target = tmp_path / "target/manual.txt"
    target.write_text("New original bytes.")
    with prepared_input(target) as ready:
        SourceStore(kb_dir).intake(ready)
    atomic_write_text(kb_dir / "wiki/concepts/proposed.md", "Manual text to review.")
    with KnowledgeWorkspace(kb_dir, other, other_parse, {"model": "test"}) as workspace:
        atomic_write_text(workspace.path / "wiki/concepts/proposed.md", cited)
        pending = workspace.proposal()
    assert pending.changes["concepts/proposed.md"] == authored.changes["concepts/owned.md"]
    assert publish_proposal(kb_dir, pending.id).status == "needs_acceptance"
    record_source_result(
        kb_dir,
        DocumentResult(
            source="other",
            status="unfinished",
            resources=(),
            input_version=other.id,
            source_id=other.source_id,
            parse_id=other_parse.id,
            source_intake="saved",
            knowledge_compilation="unfinished",
            stage="committing",
            reason="needs_acceptance",
            resume=pending.id,
        ),
    )
    preview = preview_history_cleanup(kb_dir)
    assert old.id not in preview.versions
    assert parsed.id not in preview.parses


def test_cleanup_preserves_cited_versions_and_shared_assets(kb_dir, tmp_path, model_service):
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup
    from openkb.application.source_history import source_status
    from tests.test_pageindex_source_storage import collection

    original = tmp_path / "notes.md"
    original.write_text("First version: keep the cited timeout of 42 seconds.")
    first = import_document(kb_dir, original)
    first_index = source_status(kb_dir, first.source_id)["navigation"]["pageindex"]["doc_id"]
    parsed = ParseStore(kb_dir).load(first.parse_id)
    reference = Evidence(first.source_id, first.input_version, parsed.id, parsed.blocks[0].id)
    original.write_text("Second version: separate shared input.")
    second = import_document(kb_dir, original)
    second_index = source_status(kb_dir, second.source_id)["navigation"]["pageindex"]["doc_id"]
    independent = tmp_path / "independent.md"
    independent.write_text(original.read_text())
    other = import_document(kb_dir, independent)
    original.write_text("Third version: current input.")
    current = import_document(kb_dir, original)
    (kb_dir / "wiki/concepts/citation.md").write_text(
        f"Retained evidence: {reference.version_id} / {reference.parse_id} / {reference.block_id}"
    )
    preview = preview_history_cleanup(kb_dir)
    assert second.input_version in preview.versions
    assert first.input_version not in preview.versions
    assert current.input_version not in preview.versions
    assert other.input_version not in preview.versions
    cleanup_history(kb_dir, preview.id)
    with collection(kb_dir) as documents:
        retained = {row["doc_id"] for row in documents.list_documents()}
        assert second_index not in retained
        assert first_index in retained
        assert documents.get_page_content(first_index, "1")[0]["content"].endswith("42 seconds.")
    assert "42 seconds" in read_source_evidence(kb_dir, reference, max_chars=100).text
    store = SourceStore(kb_dir)
    assert store.original(store.version(other.input_version)).read_text() == independent.read_text()
    with pytest.raises(FileNotFoundError):
        store.version(second.input_version)
    assert not preview_history_cleanup(kb_dir).files


def test_cleanup_refuses_a_preview_after_new_citations(kb_dir, tmp_path, model_service):
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup

    original = tmp_path / "notes.md"
    original.write_text("Old unreferenced material.")
    first = import_document(kb_dir, original)
    original.write_text("New retained material.")
    import_document(kb_dir, original)
    preview = preview_history_cleanup(kb_dir)
    (kb_dir / "wiki/concepts/citation.md").write_text(first.input_version)
    with pytest.raises(ValueError, match="changed"):
        cleanup_history(kb_dir, preview.id)
    assert SourceStore(kb_dir).version(first.input_version)


def test_cleanup_protects_saved_conversation_citations_and_preview_races(
    kb_dir, tmp_path, model_service
):
    from openkb.agent.chat_session import ChatSession
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup

    path = tmp_path / "chat-cited.md"
    path.write_text("Old timeout: 42 seconds.")
    old = import_document(kb_dir, path)
    path.write_text("New timeout: 43 seconds.")
    import_document(kb_dir, path)
    before = preview_history_cleanup(kb_dir)
    session = ChatSession.new(kb_dir, "offline", "en")
    session.record_turn(
        "What was the old timeout?",
        f"[Original](sources/snapshots/{old.input_version}-{old.parse_id}.md)",
        [],
    )
    with pytest.raises(ValueError, match="changed"):
        cleanup_history(kb_dir, before.id)
    after = preview_history_cleanup(kb_dir)
    assert old.input_version not in after.versions
    assert old.parse_id not in after.parses
    assert not any(old.input_version in name for name in after.files)


def test_cleanup_preview_covers_sdk_companion_files(kb_dir, tmp_path, model_service, monkeypatch):
    from pageindex.collection import Collection

    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup
    from openkb.application.source_history import source_status

    original = tmp_path / "notes.md"
    original.write_text("Old material.")
    first = import_document(kb_dir, original)
    doc_id = source_status(kb_dir, first.source_id)["navigation"]["pageindex"]["doc_id"]
    original.write_text("Current material.")
    assert import_document(kb_dir, original).knowledge_compilation == "completed"
    before = preview_history_cleanup(kb_dir)
    companion = kb_dir / ".openkb/files/default" / doc_id / "unreviewed.txt"
    companion.parent.mkdir(parents=True)
    companion.write_text("Review this managed companion before cleanup.")
    with pytest.raises(ValueError, match="changed"):
        cleanup_history(kb_dir, before.id)
    after = preview_history_cleanup(kb_dir)
    assert companion.relative_to(kb_dir).as_posix() in after.files
    delete = Collection.delete_document

    def failed_delete(collection, identity):
        delete(collection, identity)
        raise OSError("Interrupted SDK deletion")

    with monkeypatch.context() as patch:
        patch.setattr(Collection, "delete_document", failed_delete)
        with pytest.raises(OSError, match="Interrupted SDK deletion"):
            cleanup_history(kb_dir, after.id)
    assert companion.read_text() == "Review this managed companion before cleanup."
    cleanup_history(kb_dir, preview_history_cleanup(kb_dir).id)
    assert not companion.parent.exists()


def test_cleanup_retains_a_receipt_pending_its_first_compilation_index(
    kb_dir, tmp_path, model_service, monkeypatch
):
    """A current source keeps its authenticated handoff before SQLite exists."""

    from openkb.agent.compilation_index import index_path
    from openkb.agent.evidence_checkpoints import CompilationCheckpoints
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup
    from openkb.processing import DEFAULT_PROCESSING

    original = tmp_path / "pending-index.md"
    original.write_text("Current source with a resumable checkpoint.")
    result = import_document(kb_dir, original)
    store = SourceStore(kb_dir)
    source = store.version(result.input_version)
    parsed = ParseStore(kb_dir).load(result.parse_id)
    settings = {
        "model": "openai/offline",
        "processing": {
            **DEFAULT_PROCESSING,
            "context_tokens": 128_000,
            "max_context_tokens": 128_000,
            "output_tokens": 4_096,
            "max_output_tokens": 4_096,
        },
    }
    writer = CompilationCheckpoints(
        kb_dir,
        source,
        parsed,
        settings,
        None,
    )
    index_path(store, source.id).unlink()
    writer.latest.unlink(missing_ok=True)
    current = writer.key("fixture", {"stage": "facts", "text": "pending source"})

    import openkb.agent.evidence_checkpoints as checkpoints_module

    monkeypatch.setattr(
        checkpoints_module,
        "update_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index interruption")),
    )
    with pytest.raises(OSError, match="index interruption"):
        writer.save(current, {"value": "saved"})

    preview = preview_history_cleanup(kb_dir)
    cleanup_history(kb_dir, preview.id)

    reader = CompilationCheckpoints(
        kb_dir,
        source,
        parsed,
        settings,
        None,
    )
    assert current in reader.checkpoint_keys("facts")
    assert reader.load(current) == {"value": "saved"}
