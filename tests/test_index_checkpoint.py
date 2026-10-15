"""Complete PageIndex work survives compilation rollback at the application boundary."""

import json

import pytest

pytest_plugins = ("block_fixtures",)


@pytest.fixture
def indexed_failure(kb_dir, tmp_path, block_model, monkeypatch, wide_short_prompt):
    import litellm

    from ultrakb.application.documents import import_document
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.llm_execution import ModelBudgetExceeded
    from ultrakb.source_catalog import list_sources

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 4000}}
        ),
    )
    source = tmp_path / "gardening.md"
    source.write_text("hello gardeners\nPrune roses after flowering.")

    original = litellm.acompletion

    async def no_capacity(**kwargs):
        if "Write independently readable knowledge" in str(kwargs["messages"]):
            raise ModelBudgetExceeded("compile plan exceeds the remaining task allowance")
        return await original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("litellm.acompletion", no_capacity)
        with pytest.raises(ModelBudgetExceeded):
            import_document(kb_dir, source)
    admitted = list_sources(kb_dir)[0]
    return admitted, block_model


def test_complete_index_survives_rollback_and_is_reused_once(kb_dir, indexed_failure):
    from ultrakb.application.source_retry import retry_source
    from ultrakb.source_catalog import read_admission
    from ultrakb.unit_publication import list_source_units, read_head, read_unit_publication

    source, requests = indexed_failure
    markers = list((kb_dir / ".ultrakb/index-checkpoints").rglob("checkpoint.json"))
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


@pytest.mark.parametrize("generation", [{}, {"compile": {"temperature": 0.2}}])
def test_legacy_index_without_generation_options_remains_reusable(
    kb_dir, indexed_failure, generation
):
    """A retained legacy receipt omits the newer, empty options field."""
    import hashlib
    from importlib.metadata import version

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.source_retry import retry_source
    from ultrakb.config import load_config, save_config
    from ultrakb.ingest_records import UnitRevision
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.normalization import normalization_id, read_normalization
    from ultrakb.source_catalog import read_record
    from ultrakb.unit_publication import list_source_units

    source, requests = indexed_failure
    unit = list_source_units(kb_dir, source.source_id)[0]
    revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
    directory, saved = read_normalization(
        kb_dir, normalization_id(revision.source_revision_id, revision.processing_fingerprint)
    )
    policy = json.loads(saved.fingerprint)
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        connection = context.executor.bindings.public_identity()
    connection.pop("role_generation")
    assert saved.source_path is not None
    input_path = (directory / saved.source_path).with_suffix(".okbi")
    legacy = {
        "source_revision": saved.source_revision_id,
        "input": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "policy": {
            key: policy[key]
            for key in ("index_model", "index_policy", "index_sdk", "index_endpoint")
        },
        "sdk": {name: version(name) for name in ("pageindex", "pageindex-condb")},
        "connection": connection,
    }
    marker_path = next((kb_dir / ".ultrakb/index-checkpoints").rglob("checkpoint.json"))
    marker = json.loads(marker_path.read_text())
    marker["identity"] = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    marker_path.write_text(json.dumps(marker))
    retained = marker_path.read_bytes()
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        config_path = kb_dir / ".ultrakb/config.yaml"
        save_config(config_path, {**load_config(config_path), "role_generation": generation})
    context = ExecutionContext()
    before = len(requests)
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        result = retry_source(kb_dir, source.source_id, context=context)
    if generation:
        assert result.status == "failed" and "identity changed" in result.message
        assert len(requests) == before
    else:
        assert result.status == "added", result.message
        assert not any("CONTENT BLOCK STRUCTURE" in request for request in requests[before:])
    assert marker_path.read_bytes() == retained


def test_unrelated_manual_page_does_not_invalidate_document_index(kb_dir, indexed_failure):
    from ultrakb.application.proposals import list_proposals
    from ultrakb.application.source_retry import retry_source
    from ultrakb.knowledge_scope import live_scope
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.state import HashRegistry

    source, requests = indexed_failure
    manual = live_scope(kb_dir).wiki_dir / "concepts/manual-note.md"
    manual_body = (
        "# Conference-room lighting\n"
        "Use the wall switch to turn on the conference-room light.\n"
        "Keep this independent edit."
    )
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(manual, manual_body)
    marker_path = next((kb_dir / ".ultrakb/index-checkpoints").rglob("checkpoint.json"))
    retained_marker = marker_path.read_bytes()
    indexed_files = json.loads(retained_marker)["files"]
    retained_files = {
        name: HashRegistry.hash_file(marker_path.parent / "data" / name) for name in indexed_files
    }
    assert retained_files == indexed_files
    before = sum("CONTENT BLOCK STRUCTURE" in value for value in requests)
    result = retry_source(kb_dir, source.source_id)
    assert result.status == "added", result.message
    assert manual.read_bytes() == manual_body.encode("utf-8")
    assert sum("CONTENT BLOCK STRUCTURE" in value for value in requests) == before
    assert marker_path.read_bytes() == retained_marker
    assert {
        name: HashRegistry.hash_file(marker_path.parent / "data" / name) for name in indexed_files
    } == retained_files
    assert not list_proposals(kb_dir)


@pytest.mark.parametrize("rewrite_manual", [False, True])
def test_manual_page_review_is_limited_to_changed_pages(rewrite_manual):
    from ultrakb.unit_publication import protected_knowledge_pages

    manual = "concepts/manual-note.md"
    current = {manual: "human-page-digest"}
    candidate = {
        **current,
        "summaries/gardening.md": "new-summary-digest",
        "sources/gardening.okbi": "retained-index-digest",
    }
    if rewrite_manual:
        candidate[manual] = "proposed-manual-page-digest"
    assert protected_knowledge_pages(current, candidate, {}) == (
        {manual} if rewrite_manual else set()
    )


def test_corrupt_completed_index_is_not_reused_or_silently_rebuilt(kb_dir, indexed_failure):
    from ultrakb.application.source_retry import retry_source

    source, requests = indexed_failure
    path = next((kb_dir / ".ultrakb/index-checkpoints").rglob("checkpoint.json"))
    marker = json.loads(path.read_text())
    marker["files"]["index/context.sqlite"] = "0" * 64
    path.write_text(json.dumps(marker))
    before = len(requests)
    result = retry_source(kb_dir, source.source_id)
    assert result.status == "failed"
    assert "checkpoint" in result.message.lower()
    assert len(requests) == before


@pytest.mark.parametrize("dirty", [False, True])
def test_reading_a_sealed_checkpoint_does_not_make_it_unusable(kb_dir, indexed_failure, dirty):
    import sqlite3

    from ultrakb.application.source_retry import retry_source
    from ultrakb.index_location import IndexLocation
    from ultrakb.state import HashRegistry

    source, requests = indexed_failure
    marker_path = next((kb_dir / ".ultrakb/index-checkpoints").rglob("checkpoint.json"))
    retained = marker_path.read_bytes()
    marker = json.loads(retained)
    location = IndexLocation.package(marker_path.parent / "data/index")
    digest = HashRegistry.hash_file(location.database)
    with sqlite3.connect(location.database.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT doc_id FROM okb_documents WHERE doc_id=?", (marker["document_id"],)
        ).fetchone()
    connection.close()
    wal = location.database.with_name(location.database.name + "-wal")
    assert wal.is_file() and wal.stat().st_size == 0
    assert HashRegistry.hash_file(location.database) == digest
    if dirty:
        wal.write_bytes(b"Uncheckpointed writes must not be ignored")
    before = len(requests)
    result = retry_source(kb_dir, source.source_id)
    if dirty:
        assert result.status == "failed"
        assert len(requests) == before
    else:
        assert result.status == "added", result.message
        assert not any("CONTENT BLOCK STRUCTURE" in request for request in requests[before:])
    assert HashRegistry.hash_file(location.database) == digest
    assert marker_path.read_bytes() == retained


def test_withdrawn_source_cannot_restore_an_index_checkpoint(kb_dir, indexed_failure):
    from ultrakb.application.removal import preview_removal, remove_document
    from ultrakb.application.source_retry import retry_source

    source, requests = indexed_failure
    preview = preview_removal(kb_dir, source.source_id)
    remove_document(kb_dir, source.source_id, version=preview.version)
    assert not list((kb_dir / ".ultrakb/index-checkpoints").rglob("checkpoint.json"))
    before = len(requests)
    with pytest.raises(ValueError, match="current source"):
        retry_source(kb_dir, source.source_id)
    assert len(requests) == before
