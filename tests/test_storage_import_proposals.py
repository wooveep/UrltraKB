"""Compiler candidates protect human changes until exact, durable review acceptance."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.proposals import accept_proposal, list_proposals, read_proposal
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.views import storage_scope
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


async def manual_baseline(tmp_path, service, *, deleted=False):
    root, identity, unit = compile_input(tmp_path)
    (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
    first = await recompile_document(
        root, unit.source_id, unit_id=unit.unit_id, scope=storage_scope(root, identity)
    )
    assert first.status == "compiled", first
    knowledge = KnowledgeStore(root, identity)
    before = knowledge.bind()
    human = (
        None
        if deleted
        else before.read("summaries/fixture.md") + b"\n\nA reviewed human annotation.\n"
    )
    knowledge.publish(knowledge.prepare_changes(before, {"summaries/fixture.md": human}))
    return root, identity, unit, human


async def reviewed_import(tmp_path, service):
    root, identity, unit, human = await manual_baseline(tmp_path, service)
    knowledge = KnowledgeStore(root, identity)
    scope, context = storage_scope(root, identity), ExecutionContext()
    pending = await recompile_document(
        root, unit.source_id, unit_id=unit.unit_id, scope=scope, context=context
    )
    assert pending.status == "blocked" and pending.proposal_id, pending
    assert knowledge.bind().read("summaries/fixture.md") == human
    proposal = read_proposal(root, pending.proposal_id, scope=scope)
    assert proposal.status == "awaiting_confirmation"
    assert "summaries/fixture.md" in proposal.conflicts
    assert "reviewed human annotation" in proposal.diff
    return root, identity, unit, scope, context, pending, proposal


def compiler_responses():
    return [
        {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ] * 2


@pytest.mark.asyncio
async def test_compiler_cannot_silently_restore_a_manually_deleted_generated_page(tmp_path):
    with StreamTransport(compiler_responses()) as service:
        root, identity, unit, _ = await manual_baseline(tmp_path, service, deleted=True)
        scope = storage_scope(root, identity)
        result = await recompile_document(root, unit.source_id, unit_id=unit.unit_id, scope=scope)
        assert result.status == "blocked" and result.proposal_id, result
        proposal = read_proposal(root, result.proposal_id, scope=scope)
        assert "summaries/fixture.md" in proposal.conflicts
        assert "summaries/fixture.md" not in KnowledgeStore(root, identity).bind().files()
        accepted = accept_proposal(
            root, proposal.proposal_id, version=proposal.version, scope=scope
        )
        assert accepted.status == "added", accepted
        assert accepted.publication_id == KnowledgeStore(root, identity).bind().publication_id
        assert not accepted.projection_pending
        assert b"17" in KnowledgeStore(root, identity).bind().read("summaries/fixture.md")


@pytest.mark.asyncio
async def test_manual_import_changes_are_reviewable_and_accept_without_model_configuration(
    tmp_path,
):
    with StreamTransport(compiler_responses()) as service:
        root, identity, unit, scope, context, pending, proposal = await reviewed_import(
            tmp_path, service
        )
        assert [item.proposal_id for item in list_proposals(root, scope=scope)] == [
            proposal.proposal_id
        ]
        repeated = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=scope, context=context
        )
        assert repeated.proposal_id == proposal.proposal_id and repeated.status == "blocked"
        assert len(service.requests) == 4
        (root / ".env").unlink()
        (root / ".ultrakb/config.yaml").unlink()
        accepted = accept_proposal(
            root, proposal.proposal_id, version=proposal.version, scope=scope
        )
        assert accepted.status == "added", accepted
        assert accepted.units[0].successful_revision_id == unit.unit_revision_id
        assert b"human annotation" not in KnowledgeStore(root, identity).bind().read(
            "summaries/fixture.md"
        )
        completed_publication = KnowledgeStore(root, identity).bind().publication_id
        replay = accept_proposal(root, proposal.proposal_id, version=proposal.version, scope=scope)
        assert replay.status == "skipped", replay
        assert KnowledgeStore(root, identity).bind().publication_id == completed_publication
        assert len(service.requests) == 4


@pytest.mark.asyncio
async def test_import_review_reprepares_a_changed_sync_baseline_without_model_work(tmp_path):
    from ultrakb.storage.wiki_projection import ProjectionStore

    with StreamTransport(compiler_responses()) as service:
        root, identity, _, scope, _, _, proposal = await reviewed_import(tmp_path, service)
        ProjectionStore(root, identity).sync()
        (root / ".env").unlink()
        prepared = accept_proposal(
            root, proposal.proposal_id, version=proposal.version, scope=scope
        )
        assert prepared.status == "blocked", prepared
        new_id = prepared.units[0].proposal_id
        assert new_id != proposal.proposal_id
        new = read_proposal(root, new_id, scope=scope)
        assert new.diff == proposal.diff and new.version != proposal.version
        repeated = accept_proposal(
            root, proposal.proposal_id, version=proposal.version, scope=scope
        )
        assert repeated.status == "blocked" and repeated.units[0].proposal_id == new_id
        assert [item.proposal_id for item in list_proposals(root, scope=scope)] == [new_id]
        accepted = accept_proposal(root, new_id, version=new.version, scope=scope)
        assert accepted.status == "added" and not accepted.projection_pending, accepted
        assert len(service.requests) == 4


@pytest.mark.asyncio
async def test_unpublished_compiler_output_rebuilds_only_the_sync_plan_on_reentry(
    tmp_path, monkeypatch
):
    from ultrakb.storage.import_candidates import ImportCandidates
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, unit = compile_input(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    retain = ImportCandidates.retain

    def synchronize_after_ready(self, *args, **kwargs):
        candidate = retain(self, *args, **kwargs)
        ProjectionStore(root, identity).sync()
        return candidate

    with StreamTransport(compiler_responses()[:2]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as race:
            race.setattr(ImportCandidates, "retain", synchronize_after_ready)
            first = await recompile_document(
                root, unit.source_id, unit_id=unit.unit_id, scope=scope, context=context
            )
        assert first.status == "conflict", first
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=scope, context=context
        )
        assert result.status == "compiled", result
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_concurrent_acceptance_is_reported_as_the_same_committed_compiler_result(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    with StreamTransport(compiler_responses()) as service:
        root, identity, unit, scope, context, _, proposal = await reviewed_import(tmp_path, service)
        committed, accepted = KnowledgeStore.committed_import, []

        def concurrent_acceptance(self, *args, **kwargs):
            previous = committed(self, *args, **kwargs)
            if previous is None and not accepted:
                with ThreadPoolExecutor(1) as pool:
                    accepted.append(
                        pool.submit(
                            accept_proposal,
                            root,
                            proposal.proposal_id,
                            version=proposal.version,
                            scope=scope,
                        ).result(timeout=10)
                    )
            return previous

        monkeypatch.setattr(KnowledgeStore, "committed_import", concurrent_acceptance)
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=scope, context=context
        )
        assert accepted[0].status == "added", accepted
        assert (
            result.status == "compiled" and result.knowledge_revision_id == proposal.proposal_id
        ), result
        assert len(service.requests) == 4


@pytest.mark.asyncio
async def test_lost_import_candidate_acknowledgement_recovers_before_compiler_replay(
    tmp_path, monkeypatch
):
    from ultrakb.storage.import_candidates import ImportCandidates

    retain = ImportCandidates.retain

    def lost_acknowledgement(self, *args, **kwargs):
        retain(self, *args, **kwargs)
        raise OSError("Import candidate acknowledgement lost")

    with StreamTransport(compiler_responses()) as service:
        root, identity, unit, human = await manual_baseline(tmp_path, service)
        scope, context = storage_scope(root, identity), ExecutionContext()
        with monkeypatch.context() as fault:
            fault.setattr(ImportCandidates, "retain", lost_acknowledgement)
            interrupted = await recompile_document(
                root, unit.source_id, unit_id=unit.unit_id, scope=scope, context=context
            )
        assert interrupted.status == "blocked"
        proposal = list_proposals(root, scope=scope)[0]
        restored = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=scope, context=context
        )
        assert restored.proposal_id == proposal.proposal_id
        assert KnowledgeStore(root, identity).bind().read("summaries/fixture.md") == human
        assert len(service.requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["before-commit", "lost-ack", "sync", "persistent-read"])
async def test_import_review_recovers_exact_publication_and_reports_worktree_failure(
    tmp_path, monkeypatch, fault
):
    from ultrakb.application import storage_proposals
    from ultrakb.storage.knowledge_store import PublicationConflict

    with StreamTransport(compiler_responses()) as service:
        root, identity, unit, scope, _, _, proposal = await reviewed_import(tmp_path, service)
        knowledge = KnowledgeStore(root, identity)
        _, candidate = storage_proposals._candidate(scope, proposal.proposal_id)
        with pytest.raises(PublicationConflict, match="reviewed"):
            knowledge.publish(candidate["prepared"])
        publish, moved = KnowledgeStore.publish, []

        def interrupted_publish(self, *args, **kwargs):
            if fault == "before-commit":
                raise OSError("Publication not committed")
            publication = publish(self, *args, **kwargs)
            if fault == "lost-ack":
                raise OSError("Publication acknowledgement lost")
            if fault == "persistent-read":
                shm = next(root.rglob("kb.sqlite-shm"))
                unavailable = shm.with_suffix(".unavailable")
                shm.rename(unavailable)
                moved.append((shm, unavailable))
            return publication

        def interrupted_sync(*args, **kwargs):
            raise OSError("Worktree unavailable")

        try:
            with monkeypatch.context() as failure:
                if fault == "sync":
                    failure.setattr(storage_proposals, "sync_publication", interrupted_sync)
                else:
                    failure.setattr(KnowledgeStore, "publish", interrupted_publish)
                first = accept_proposal(
                    root, proposal.proposal_id, version=proposal.version, scope=scope
                )
        finally:
            for shm, unavailable in moved:
                unavailable.rename(shm)
        assert first.status == ("failed" if fault == "before-commit" else "added"), first
        if fault in {"sync", "persistent-read"}:
            assert first.resources == ("wiki",) and "synchronize wiki" in first.unfinished
            assert first.projection_pending and first.publication_id is not None
        recovered = accept_proposal(
            root, proposal.proposal_id, version=proposal.version, scope=scope
        )
        assert recovered.status == ("added" if fault == "before-commit" else "skipped"), recovered
        assert not recovered.unfinished
        assert recovered.units[0].knowledge_revision_id == proposal.proposal_id
        assert len(service.requests) == 4


@pytest.mark.asyncio
async def test_import_review_rejects_a_stale_publication_without_sending_or_losing_the_candidate(
    tmp_path,
):
    with StreamTransport(compiler_responses()) as service:
        root, identity, unit, scope, context, pending, proposal = await reviewed_import(
            tmp_path, service
        )
        knowledge = KnowledgeStore(root, identity)
        current = knowledge.bind()
        knowledge.publish(
            knowledge.prepare_changes(current, {"concepts/new.md": b"# New human content\n"})
        )
        result = accept_proposal(
            root,
            proposal.proposal_id,
            version=proposal.version,
            scope=storage_scope(root, identity),
        )
        assert result.status == "blocked", result
        assert read_proposal(root, proposal.proposal_id, scope=scope).status == "superseded"
        assert b"human annotation" in knowledge.bind().read("summaries/fixture.md")
        assert len(service.requests) == 4
