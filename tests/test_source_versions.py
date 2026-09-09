"""Immutable provenance, checkpoints and bounded evidence used by document imports."""

import pytest

from openkb.inputs import prepared_input
from openkb.sources import SourceStore


def intake(kb, source):
    with prepared_input(source) as ready:
        return SourceStore(kb).intake(ready)


def test_source_identity_is_independent_of_shared_bytes(kb_dir, tmp_path):
    one, two = tmp_path / "one.md", tmp_path / "two.md"
    one.write_text("Same bytes")
    two.write_bytes(one.read_bytes())
    store = SourceStore(kb_dir)
    first, other = intake(kb_dir, one), intake(kb_dir, two)
    assert first.source_id != other.source_id
    assert first.blob == other.blob
    assert intake(kb_dir, one) == first
    one.write_text("Changed content")
    updated = intake(kb_dir, one)
    assert updated.source_id == first.source_id and updated.id != first.id
    assert store.original(first).read_text() == "Same bytes"
    assert store.original(updated).read_text() == "Changed content"
    one.write_text("Same bytes")
    reverted = intake(kb_dir, one)
    assert reverted.id != first.id and reverted.revision == updated.revision + 1
    assert reverted.blob == first.blob


def test_sidecar_changes_create_versions_without_overwriting_old_assets(kb_dir, tmp_path):
    source, image = tmp_path / "manual.md", tmp_path / "figure.png"
    source.write_text("![Figure](figure.png)")
    image.write_bytes(b"first image")
    before = intake(kb_dir, source)
    image.write_bytes(b"second image")
    after = intake(kb_dir, source)
    assert before.blob == after.blob and before.id != after.id
    store = SourceStore(kb_dir)
    assert store.asset(before.assets["figure.png"]).read_bytes() == b"first image"
    image.unlink()
    missing = intake(kb_dir, source)
    assert missing.assets["figure.png"] is None and missing.id != after.id


def test_failed_knowledge_transaction_does_not_undo_source_intake(kb_dir, tmp_path):
    from openkb.locks import atomic_write_text, kb_ingest_lock
    from openkb.mutation import mutation_scope

    source = tmp_path / "original.txt"
    source.write_text("Retained before knowledge work")
    version = intake(kb_dir, source)
    target = kb_dir / "wiki/entities/proposal.md"
    with pytest.raises(RuntimeError), kb_ingest_lock(kb_dir / ".openkb"):
        with mutation_scope(kb_dir, [target], operation="test knowledge failure"):
            atomic_write_text(target, "Partial knowledge")
            raise RuntimeError("Failed after intake")
    assert not target.exists()
    assert SourceStore(kb_dir).original(version).read_text() == source.read_text()


def test_corrupt_immutable_blob_is_rejected_instead_of_replaced(kb_dir, tmp_path):
    source = tmp_path / "original.txt"
    source.write_text("Original")
    version = intake(kb_dir, source)
    store = SourceStore(kb_dir)
    store.original(version).write_text("Corrupted")
    with pytest.raises(ValueError, match="digest"):
        intake(kb_dir, source)


def test_shared_import_retains_original_when_compiler_is_unfinished(kb_dir, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import litellm

    from openkb.application.documents import import_document

    source = tmp_path / "manual.md"
    source.write_text("Original survives incomplete knowledge")
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="invalid plan"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    result = import_document(kb_dir, source)
    assert result.status == "unfinished"
    assert result.source_intake == "saved"
    assert result.source_id is not None and result.input_version is not None
    saved = SourceStore(kb_dir).version(result.input_version)
    assert saved.source_id == result.source_id
    assert SourceStore(kb_dir).original(saved).read_text() == source.read_text()
    from openkb.application.knowledge_bases import get_kb_list

    documents = get_kb_list(kb_dir)["documents"]
    assert len(documents) == 1
    assert documents[0]["source_id"] == result.source_id
    assert documents[0]["knowledge_compilation"] == "unfinished"


def test_shared_import_compiles_independent_sources_and_skips_only_completed_version(
    kb_dir, tmp_path, model_service
):
    from openkb.application.documents import import_document
    from openkb.state import HashRegistry

    first, second = tmp_path / "one.md", tmp_path / "two.md"
    first.write_text("The same bytes from two distinct sources.")
    second.write_bytes(first.read_bytes())
    one = import_document(kb_dir, first)
    two = import_document(kb_dir, second)
    assert one.status == two.status == "added", (one, two)
    assert one.source_id != two.source_id
    assert one.parse_id == two.parse_id
    assert len(HashRegistry(kb_dir / ".openkb/hashes.json").all_entries()) == 2
    calls = len(model_service)
    repeated = import_document(kb_dir, first)
    assert repeated.status == "skipped" and len(model_service) == calls


def test_import_preserves_saved_original_but_rejects_source_edit_before_publication(
    kb_dir, tmp_path, model_service
):
    from openkb.application.documents import import_document
    from openkb.knowledge_commit import wiki_version

    source = tmp_path / "manual.txt"
    source.write_text("Original version")
    before = wiki_version(kb_dir)

    def edit_on_commit(event):
        if event.get("stage") == "committing":
            source.write_text("User's newer version")

    result = import_document(kb_dir, source, on_event=edit_on_commit)
    assert result.status == "unfinished" and result.reason == "input_conflict"
    assert wiki_version(kb_dir) == before
    saved = SourceStore(kb_dir).version(result.input_version)
    assert SourceStore(kb_dir).original(saved).read_text() == "Original version"


@pytest.mark.parametrize("history_fails", [False, True])
def test_accepting_reviewed_import_publishes_saved_proposal_without_repeating_model_work(
    kb_dir, tmp_path, model_service, monkeypatch, history_fails
):
    from openkb.application.documents import import_document
    from openkb.application.source_actions import continue_source, review_source_proposal

    source = tmp_path / "manual.txt"
    source.write_text("Original version")
    index = kb_dir / "wiki/index.md"
    index.write_text("# Human index\nKeep this note.\n")
    result = import_document(kb_dir, source)
    assert result.reason == "needs_acceptance"
    review = review_source_proposal(kb_dir, result.resume)
    assert "index.md" in review["protected"]
    assert "Keep this note" in review["diffs"]["index.md"]
    calls = len(model_service)
    if history_fails:

        def failed_history(*args):
            raise OSError("Synthetic history disk failure")

        monkeypatch.setattr(
            "openkb.application.source_history.record_source_result", failed_history
        )
    completed = continue_source(
        kb_dir,
        result.source_id,
        version_id=result.input_version,
        proposal_id=result.resume,
        accept_pages=review["protected"],
    )
    assert completed.status == "added"
    if history_fails:
        assert "source_outcome_record_failed" in completed.warnings
    assert len(model_service) == calls


def test_recompilation_preserves_manual_metadata_and_uses_saved_source(
    kb_dir, tmp_path, model_service
):
    import asyncio

    from openkb.application.documents import import_document
    from openkb.application.recompilation import recompile_document
    from openkb.state import HashRegistry

    source = tmp_path / "manual.txt"
    source.write_text("Original")
    first = import_document(kb_dir, source)
    entry = HashRegistry(kb_dir / ".openkb/hashes.json").get(first.source_id)
    summary = kb_dir / "wiki/summaries" / (entry["doc_name"] + ".md")
    summary.write_text("---\ntitle: Human annotation\n---\nHuman content")
    source.unlink()
    result = asyncio.run(recompile_document(kb_dir, first.source_id))
    assert result.status == "unfinished" and result.message == "needs_acceptance"
    assert summary.read_text().endswith("Human content")
    assert result.document.source_intake == "saved"


def test_import_rejects_redirected_source_storage_before_writing_blobs(kb_dir, tmp_path_factory):
    import os

    from openkb.application.documents import import_document

    if os.name == "nt":
        pytest.skip("POSIX symlink fixture")
    outside = tmp_path_factory.mktemp("outside-source-store")
    (kb_dir / ".openkb/source-store").symlink_to(outside, target_is_directory=True)
    source = kb_dir / "input.txt"
    source.write_text("Retained only inside the intended knowledge base")
    with pytest.raises(ValueError):
        import_document(kb_dir, source)
    assert list(outside.iterdir()) == []


def test_post_commit_observer_failure_keeps_completed_outcome(kb_dir, tmp_path, model_service):
    from openkb.application.documents import import_document

    original = tmp_path / "observer.md"
    original.write_text("Original evidence")

    def observer(event):
        if event.get("stage") == "committed":
            raise RuntimeError("observer disconnected after commit")

    result = import_document(kb_dir, original, on_event=observer)
    assert result.knowledge_compilation == "completed"
    assert "commit_observer_unavailable" in result.warnings
    assert len(list((kb_dir / "wiki/summaries").glob("observer-*.md"))) == 1
    again = import_document(kb_dir, original)
    assert again.status == "skipped" and again.knowledge_compilation == "completed"
    assert len(model_service) == 2


def test_document_parser_uses_global_settings_and_kb_override(
    kb_dir, tmp_path, monkeypatch, model_service
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_global_config_patch, apply_kb_config_patch
    from openkb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
    from openkb.evidence import ParseStore

    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_PATH", tmp_path / "global/global.yaml")
    apply_global_config_patch(
        GlobalConfigPatchRequest(config={"parsing": {"ocr": {"backend": "cloud"}}})
    )
    source = tmp_path / "settings.md"
    source.write_text("Native text requires no OCR installation")
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed"
    assert ParseStore(kb_dir).load(first.parse_id).profile["ocr"]["backend"] == "cloud"
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"parsing": {"ocr": {"backend": "local"}}}),
    )
    second = import_document(kb_dir, source)
    assert second.source_id == first.source_id and second.input_version == first.input_version
    assert second.parse_id != first.parse_id
    assert ParseStore(kb_dir).load(second.parse_id).profile["ocr"]["backend"] == "local"
