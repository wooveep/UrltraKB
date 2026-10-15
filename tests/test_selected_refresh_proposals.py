"""Saved selected-refresh proposals preserve retained input and manual-edit identity."""

import asyncio
import json

import pytest
from test_selected_refresh_sdk import _import, _prepared

pytest_plugins = ("test_vendor_sdk",)


def _proposal(kb, service):
    from ultrakb.application.pages import read_page, save_page
    from ultrakb.application.refresh import refresh_selected_sources

    sources, scope = _prepared(kb, service)
    for name in ("concepts/shared", "summaries/normal"):
        page = read_page(kb, name, scope=scope)
        save_page(
            kb,
            page.path,
            page.body + "\nHuman review note.\n",
            version=page.version,
            scope=scope,
        )
    result = asyncio.run(refresh_selected_sources(kb, sources, scope=scope))
    assert result.status == "awaiting_confirmation", result.message
    path = kb / ".ultrakb/refresh-proposals" / result.proposal_id / "proposal.json"
    return scope, result, path


@pytest.mark.parametrize("superseded", [False, True])
def test_selected_proposal_preserves_manual_baseline_and_rejects_late_acceptance(
    kb_dir, model_service, superseded
):
    from ultrakb.application.refresh import accept_refresh_proposal, read_refresh_proposal
    from ultrakb.unit_publication import read_head, wiki_versions

    scope, result, _ = _proposal(kb_dir, model_service)
    if superseded:
        _import(kb_dir, model_service, "first", "The first policy requires joint review.")
    before = read_head(kb_dir, scope.view_id)
    pages = wiki_versions(kb_dir, scope.wiki_dir)
    manual = scope.wiki_dir / "summaries/normal.md"
    manual_bytes = manual.read_bytes()
    assert before.generated_baselines["summaries/normal.md"] != pages["summaries/normal.md"]
    count = len(model_service.requests)

    review = read_refresh_proposal(kb_dir, result.proposal_id, scope=scope)
    assert review["status"] == ("superseded" if superseded else "awaiting_confirmation")
    accepted = accept_refresh_proposal(
        kb_dir, result.proposal_id, version=review["version"], scope=scope
    )

    after = read_head(kb_dir, scope.view_id)
    assert accepted.status == ("conflict" if superseded else "partial")
    assert after.inputs == before.inputs
    assert (
        after.generated_baselines["summaries/normal.md"]
        == before.generated_baselines["summaries/normal.md"]
    )
    assert manual.read_bytes() == manual_bytes
    assert len(model_service.requests) == count
    if superseded:
        assert after == before
        assert wiki_versions(kb_dir, scope.wiki_dir) == pages
    else:
        assert after.knowledge_revision_id != before.knowledge_revision_id
        assert "summaries/normal.md" in after.needs_refresh
        assert "concepts/shared.md" not in after.needs_refresh
        shared = (scope.wiki_dir / "concepts/shared.md").read_text()
        assert "The first policy requires review." in shared
        assert "Human review note." in shared


@pytest.mark.parametrize("superseded", [False, True])
@pytest.mark.parametrize("mutation", ["drop_normal_input", "stamp_manual_baseline", "page_scope"])
def test_selected_proposal_rejects_changed_retained_metadata_even_when_superseded(
    kb_dir, model_service, mutation, superseded
):
    from ultrakb.application.refresh import accept_refresh_proposal, read_refresh_proposal
    from ultrakb.ingest_records import ImportUnit
    from ultrakb.source_catalog import read_record
    from ultrakb.state import HashRegistry
    from ultrakb.unit_publication import read_head, wiki_versions

    scope, result, path = _proposal(kb_dir, model_service)
    data = json.loads(path.read_text())
    head = read_head(kb_dir, scope.view_id)
    normal_unit = next(
        unit
        for unit in head.inputs
        if read_record(kb_dir, "units", unit, ImportUnit).doc_name == "normal"
    )
    if mutation == "drop_normal_input":
        removed = data["inputs"].pop(normal_unit)
        data["candidate_manifest"]["input_revisions"].remove(removed)
    elif mutation == "stamp_manual_baseline":
        data["candidate_manifest"]["generated_baselines"]["summaries/normal.md"] = data[
            "candidate_pages"
        ]["summaries/normal.md"]
    else:
        data["selection"]["page_paths"] = ["summaries/first.md"]
    # Corrupt only the disposable saved proposal. A fresh file digest must not
    # authorize altered source identity or silently claim manual bytes as generated.
    path.write_text(json.dumps(data))
    if superseded:
        _import(kb_dir, model_service, "first", "The first policy requires joint review.")
    before = read_head(kb_dir, scope.view_id)
    pages = wiki_versions(kb_dir, scope.wiki_dir)
    count = len(model_service.requests)

    error = (
        "selected page scope"
        if mutation == "page_scope"
        else "retained inputs or generated baselines"
    )
    with pytest.raises(ValueError, match=error):
        read_refresh_proposal(kb_dir, result.proposal_id, scope=scope)
    with pytest.raises(ValueError, match=error):
        accept_refresh_proposal(
            kb_dir, result.proposal_id, version=HashRegistry.hash_file(path), scope=scope
        )

    assert read_head(kb_dir, scope.view_id) == before
    assert wiki_versions(kb_dir, scope.wiki_dir) == pages
    assert len(model_service.requests) == count


def test_scoped_proposal_cannot_modify_index_without_global_resolution(kb_dir, model_service):
    from ultrakb.application.pages import read_page, save_page
    from ultrakb.application.refresh import (
        accept_refresh_proposal,
        read_refresh_proposal,
        refresh_selected_sources,
    )
    from ultrakb.ingest_records import KnowledgeHead
    from ultrakb.refresh_publication import refresh_baselines
    from ultrakb.state import HashRegistry
    from ultrakb.unit_publication import read_head, wiki_versions

    sources, scope = _prepared(kb_dir, model_service)
    page = read_page(kb_dir, "concepts/shared", scope=scope)
    save_page(
        kb_dir, page.path, page.body + "\nHuman review note.\n", version=page.version, scope=scope
    )
    result = asyncio.run(
        refresh_selected_sources(
            kb_dir,
            sources,
            scope=scope,
            page_paths=["concepts/shared.md"],
        )
    )
    assert result.status == "awaiting_confirmation", result.message
    directory = kb_dir / ".ultrakb/refresh-proposals" / result.proposal_id
    path = directory / "proposal.json"
    proposal = json.loads(path.read_text())
    assert proposal["selection"]["page_paths"] == ["concepts/shared.md"]
    assert "index.md" not in proposal["selection"]["resolved"]
    index = directory / "wiki/index.md"
    from ultrakb.locks import atomic_write_text

    atomic_write_text(index, index.read_text() + "\nUnselected injected index content.\n")
    proposal["candidate_pages"]["index.md"] = HashRegistry.hash_file(index)
    proposal["candidate_manifest"]["generated_baselines"] = refresh_baselines(
        KnowledgeHead.model_validate_json(json.dumps(proposal["expected_head"])),
        proposal["expected_pages"],
        proposal["candidate_pages"],
        retain_unchanged=True,
    )
    path.write_text(json.dumps(proposal))
    before = read_head(kb_dir, scope.view_id)
    old_files = wiki_versions(kb_dir, scope.wiki_dir)
    count = len(model_service.requests)
    # Fresh proposal bytes are not authority to expand the explicit page selection.
    with pytest.raises(ValueError):
        read_refresh_proposal(kb_dir, result.proposal_id, scope=scope)
    with pytest.raises(ValueError):
        accept_refresh_proposal(
            kb_dir,
            result.proposal_id,
            version=HashRegistry.hash_file(path),
            scope=scope,
        )
    assert read_head(kb_dir, scope.view_id) == before
    assert wiki_versions(kb_dir, scope.wiki_dir) == old_files
    assert len(model_service.requests) == count
