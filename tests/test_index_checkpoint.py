"""Complete PageIndex work survives compilation rollback at the application boundary."""

import json

import pytest

pytest_plugins = ("block_fixtures",)


@pytest.fixture
def indexed_failure(kb_dir, tmp_path, block_model, monkeypatch):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.llm_execution import ModelBudgetExceeded
    from openkb.source_catalog import list_sources

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    source = tmp_path / "gardening.md"
    source.write_text("hello gardeners\nPrune roses after flowering.")

    async def no_capacity(*args, **kwargs):
        raise ModelBudgetExceeded("compile plan exceeds the remaining task allowance")

    with monkeypatch.context() as patch:
        patch.setattr("openkb.agent.compiler.compile_long_doc", no_capacity)
        with pytest.raises(ModelBudgetExceeded):
            import_document(kb_dir, source)
    admitted = list_sources(kb_dir)[0]
    return admitted, block_model


def test_complete_index_survives_rollback_and_is_reused_once(kb_dir, indexed_failure):
    from openkb.application.source_retry import retry_source
    from openkb.source_catalog import read_admission
    from openkb.unit_publication import list_source_units, read_head, read_unit_publication

    source, requests = indexed_failure
    markers = list((kb_dir / ".openkb/index-checkpoints").rglob("checkpoint.json"))
    assert len(markers) == 1
    assert read_head(kb_dir).knowledge_revision_id is None
    unit = list_source_units(kb_dir, source.source_id)[0]
    failed = read_unit_publication(kb_dir, unit.unit_id)
    assert failed.completed_stages == ("conversion", "indexing")
    assert "Index complete; compilation incomplete" in failed.message
    assert (kb_dir / failed.recovery_point).is_file()
    assert (kb_dir / read_admission(kb_dir, source).revision.original).is_file()
    indexed_calls = sum("CONTENT BLOCK STRUCTURE" in value for value in requests)
    result = retry_source(kb_dir, source.source_id)
    assert result.status == "added", result.message
    assert sum("CONTENT BLOCK STRUCTURE" in value for value in requests) == indexed_calls
    published = result.units[0].knowledge_revision_id
    again = retry_source(kb_dir, source.source_id)
    assert again.status == "skipped"
    assert again.units[0].knowledge_revision_id == published


def test_unrelated_manual_page_does_not_invalidate_document_index(kb_dir, indexed_failure):
    from openkb.application.source_retry import retry_source
    from openkb.knowledge_scope import live_scope
    from openkb.locks import atomic_write_text, kb_ingest_lock

    source, requests = indexed_failure
    manual = live_scope(kb_dir).wiki_dir / "concepts/manual-note.md"
    with kb_ingest_lock(kb_dir / ".openkb"):
        atomic_write_text(manual, "# A gardener's manual note\nKeep this independent edit.")
    before = sum("CONTENT BLOCK STRUCTURE" in value for value in requests)
    result = retry_source(kb_dir, source.source_id)
    assert result.status == "added", result.message
    assert "Keep this independent edit" in manual.read_text()
    assert sum("CONTENT BLOCK STRUCTURE" in value for value in requests) == before


def test_corrupt_completed_index_is_not_reused_or_silently_rebuilt(kb_dir, indexed_failure):
    from openkb.application.source_retry import retry_source

    source, requests = indexed_failure
    path = next((kb_dir / ".openkb/index-checkpoints").rglob("checkpoint.json"))
    marker = json.loads(path.read_text())
    marker["files"]["index/context.sqlite"] = "0" * 64
    path.write_text(json.dumps(marker))
    before = len(requests)
    result = retry_source(kb_dir, source.source_id)
    assert result.status == "failed"
    assert "checkpoint" in result.message.lower()
    assert len(requests) == before


def test_withdrawn_source_cannot_restore_an_index_checkpoint(kb_dir, indexed_failure):
    from openkb.application.removal import preview_removal, remove_document
    from openkb.application.source_retry import retry_source

    source, requests = indexed_failure
    preview = preview_removal(kb_dir, source.source_id)
    remove_document(kb_dir, source.source_id, version=preview.version)
    assert not list((kb_dir / ".openkb/index-checkpoints").rglob("checkpoint.json"))
    before = len(requests)
    with pytest.raises(ValueError, match="current source"):
        retry_source(kb_dir, source.source_id)
    assert len(requests) == before
