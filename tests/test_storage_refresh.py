"""Refresh publishes a coherent rebuilt view or retains an exact review candidate."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.pages import read_page, save_page
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.refresh import (
    accept_refresh_proposal,
    list_refresh_proposals,
    read_refresh_proposal,
    refresh_knowledge_view,
    refresh_status,
)
from ultrakb.application.removal import remove_document
from ultrakb.application.views import storage_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def responses(value=17):
    return [
        {"content": json.dumps({"description": "Fixture", "content": f"The value is {value}."})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]


async def compiled(root, identity, unit):
    result = await recompile_document(
        root, unit.source_id, unit_id=unit.unit_id, scope=storage_scope(root, identity)
    )
    assert result.status == "compiled", result.message
    return storage_scope(root, identity)


@pytest.mark.asyncio
async def test_empty_refresh_excludes_withdrawn_inputs_without_model_configuration(tmp_path):
    root, identity, unit = compile_input(tmp_path)
    with StreamTransport(responses()) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        initial = await compiled(root, identity, unit)
        removed = remove_document(root, unit.source_id, scope=initial)
        assert removed.status == "removed"
        before = storage_scope(root, identity)
        assert refresh_status(root, scope=before)["needs_refresh"]
        (root / ".ultrakb/config.yaml").unlink()
        result = await refresh_knowledge_view(root, scope=before)
        assert result.status == "completed", result
        current = storage_scope(root, identity)
        assert current.knowledge.publication.revision_id == result.knowledge_revision_id
        assert current.knowledge.unit_revisions() == ()
        assert "summaries/fixture.md" not in current.knowledge.files()
        assert not refresh_status(root, scope=current)["needs_refresh"]
        assert "17" in read_page(root, "summaries/fixture.md", scope=initial).body
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_full_refresh_rebuilds_retained_inputs_and_keeps_prior_publications(tmp_path):
    root, identity, unit = compile_input(tmp_path)
    with StreamTransport([*responses(), *responses()]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        before = await compiled(root, identity, unit)
        context = ExecutionContext()
        result = await refresh_knowledge_view(root, scope=before, context=context)
        assert result.status == "completed", result
        current = storage_scope(root, identity)
        assert current.publication_id != before.publication_id
        assert current.knowledge.unit_revisions() == (unit.unit_revision_id,)
        assert current.knowledge.input_units(page="AGENTS.md") == ()
        assert "17" in read_page(root, "summaries/fixture.md", scope=current).body
        assert "17" in read_page(root, "summaries/fixture.md", scope=before).body
        assert not refresh_status(root, scope=current)["needs_refresh"]
        assert len(service.requests) == 4
        from ultrakb.application.knowledge_bases import get_kb_status

        status = get_kb_status(root, scope=current)
        assert status["last_compile"] == status["published_at"]


@pytest.mark.asyncio
async def test_refresh_preserves_and_uses_the_fixed_custom_schema(tmp_path):
    from ultrakb.schema import AGENTS_MD

    root, identity, unit = compile_input(tmp_path)
    with StreamTransport([*responses(), *responses()]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        before = await compiled(root, identity, unit)
        schema = (AGENTS_MD + "\nCUSTOM_SCHEMA_SENTINEL\n").encode()
        store = KnowledgeStore(root, identity)
        store.publish(store.prepare_changes(before.knowledge, {"AGENTS.md": schema}))
        selected = storage_scope(root, identity)
        result = await refresh_knowledge_view(root, scope=selected)
        assert result.status == "completed", result
        assert storage_scope(root, identity).knowledge.read("AGENTS.md") == schema
        assert "CUSTOM_SCHEMA_SENTINEL" in json.dumps(service.requests[2:])
        assert len(service.requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["new_source", "new_unit", "withdrawal"])
async def test_refresh_rechecks_catalog_before_another_model_send(tmp_path, change):
    root, identity, unit = compile_input(tmp_path)
    scope = None
    expected_head = []
    response_values = responses()

    def respond(request, index):
        if index == 2:
            catalog = CatalogStore(root, identity)
            if change == "new_source":
                original = tmp_path / "later.txt"
                original.write_text("Another source")
                catalog.admit(original, source_key="later")
            elif change == "new_unit":
                catalog.plan_unit(unit.processing_id, key="later", doc_name="later")
            else:
                result = remove_document(root, unit.source_id, scope=scope)
                assert result.status == "removed", result
            expected_head.append(storage_scope(root, identity).publication_id)
        return response_values[index % 2]

    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        scope = await compiled(root, identity, unit)
        result = await refresh_knowledge_view(root, scope=scope)
        assert result.status == "conflict", result
        assert storage_scope(root, identity).publication_id == expected_head[0]
        assert len(service.requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["accepted_summary", "publication"])
async def test_refresh_resumes_durable_results_without_repeating_paid_requests(
    tmp_path, monkeypatch, interrupt
):
    from ultrakb.storage.checkpoint_store import CompileWork

    root, identity, unit = compile_input(tmp_path)
    with StreamTransport([*responses(), *responses()]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        before = await compiled(root, identity, unit)
        context = ExecutionContext()
        with monkeypatch.context() as crash:
            if interrupt == "accepted_summary":
                save = CompileWork.save

                def interrupted_save(self, key, *args, **kwargs):
                    if key == "short-summary":
                        raise OSError("Checkpoint delivery interrupted")
                    return save(self, key, *args, **kwargs)

                crash.setattr(CompileWork, "save", interrupted_save)
            else:

                def interrupted_publish(self, prepared, **kwargs):
                    raise OSError("Publication interrupted")

                crash.setattr(KnowledgeStore, "publish", interrupted_publish)
            result = await refresh_knowledge_view(root, scope=before, context=context)
        assert result.status == "failed", result
        assert len(service.requests) == (3 if interrupt == "accepted_summary" else 4)
        assert storage_scope(root, identity).publication_id == before.publication_id
        recovered = await refresh_knowledge_view(root, scope=before, context=context)
        assert recovered.status == "completed", recovered
        assert len(service.requests) == 4
        assert storage_scope(root, identity).publication_id == recovered.publication_id
    assert not (root / ".ultrakb/refresh-proposals").exists()
    from ultrakb.application.knowledge_tasks import read_knowledge_execution

    detail = read_knowledge_execution(root, context.storage_execution.execution_id)
    assert detail.execution.state == "completed"
    assert [row.publication_id for row in detail.publications] == [recovered.publication_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("after_review", ["unchanged", "publication"])
async def test_manual_refresh_diff_is_retained_and_acceptance_rechecks_its_basis(
    tmp_path, after_review
):
    root, identity, unit = compile_input(tmp_path)
    with StreamTransport([*responses(), *responses()]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        before = await compiled(root, identity, unit)
        page = read_page(root, "summaries/fixture.md", scope=before)
        saved = save_page(
            root,
            "summaries/fixture",
            "Manual explanation retained for review.",
            version=page.version,
            publication_id=before.publication_id,
            scope=before,
        )
        assert saved.status == "saved", saved
        manual = storage_scope(root, identity)
        result = await refresh_knowledge_view(root, scope=manual)
        assert result.status == "awaiting_confirmation", result
        assert storage_scope(root, identity).publication_id == manual.publication_id
        proposal = read_refresh_proposal(root, result.proposal_id, scope=manual)
        assert "summaries/fixture.md" in proposal["conflicts"]
        assert "Manual explanation" in proposal["diff"] and "The value is 17." in proposal["diff"]
        assert result.proposal_id in {
            row["proposal_id"] for row in list_refresh_proposals(root, scope=manual)
        }
        if after_review == "publication":
            store = KnowledgeStore(root, identity)
            store.publish(store.prepare_changes(manual.knowledge, {"reports/later.md": b"Later"}))
        head = storage_scope(root, identity).publication_id
        accepted = accept_refresh_proposal(
            root,
            result.proposal_id,
            version=proposal["version"],
            scope=storage_scope(root, identity),
        )
        if after_review == "unchanged":
            assert accepted.status == "completed", accepted
            assert (
                "17"
                in read_page(root, "summaries/fixture.md", scope=storage_scope(root, identity)).body
            )
            replayed = accept_refresh_proposal(
                root,
                result.proposal_id,
                version=proposal["version"],
                scope=storage_scope(root, identity),
            )
            assert replayed.knowledge_revision_id == accepted.knowledge_revision_id
        else:
            assert accepted.status == "conflict", accepted
            assert storage_scope(root, identity).publication_id == head
        assert "Manual explanation" in read_page(root, "summaries/fixture.md", scope=manual).body
        assert len(service.requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["acknowledgement", "projection", "observation", "cleanup"])
async def test_refresh_keeps_committed_success_when_later_confirmation_fails(
    tmp_path, monkeypatch, interrupt
):
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, unit = compile_input(tmp_path)
    with StreamTransport([*responses(), *responses()]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        before = await compiled(root, identity, unit)
        context = ExecutionContext()
        with monkeypatch.context() as crash:
            if interrupt == "acknowledgement":
                publish = KnowledgeStore.publish

                def lost_acknowledgement(self, prepared, **kwargs):
                    publish(self, prepared, **kwargs)
                    raise OSError("Publication acknowledgement lost")

                crash.setattr(KnowledgeStore, "publish", lost_acknowledgement)
            elif interrupt == "projection":

                def interrupted_sync(self, **kwargs):
                    raise OSError("Projection needs recovery")

                crash.setattr(ProjectionStore, "sync", interrupted_sync)
            elif interrupt == "observation":
                finish_stage = ExecutionStore.finish_stage
                interrupted = False

                def interrupted_observation(self, *args, **kwargs):
                    nonlocal interrupted
                    finish_stage(self, *args, **kwargs)
                    if not interrupted and KnowledgeStore(root, identity).bind().publication_id != (
                        before.publication_id
                    ):
                        interrupted = True
                        raise OSError("Publication observation acknowledgement lost")

                crash.setattr(ExecutionStore, "finish_stage", interrupted_observation)
            else:
                finish = ExecutionStore.finish

                def interrupted_cleanup(self, *args, **kwargs):
                    finish(self, *args, **kwargs)
                    raise OSError("Execution cleanup acknowledgement lost")

                crash.setattr(ExecutionStore, "finish", interrupted_cleanup)
            result = await refresh_knowledge_view(root, scope=before, context=context)
        assert result.status == "completed", result
        assert result.publication_id == storage_scope(root, identity).publication_id
        assert result.projection_pending == (interrupt == "projection")
        recovered = await refresh_knowledge_view(root, scope=before, context=context)
        assert recovered.status == "completed" and not recovered.projection_pending
        assert recovered.publication_id == result.publication_id
        assert len(service.requests) == 4


@pytest.mark.asyncio
async def test_refresh_rebuilds_every_retained_unit_into_one_publication(tmp_path):
    root, identity, first = compile_input(tmp_path)
    second = CatalogStore(root, identity).plan_unit(
        first.processing_id, key="second", doc_name="second"
    )
    with StreamTransport([*responses(), *responses(), *responses(), *responses()]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        await compiled(root, identity, first)
        before = await compiled(root, identity, second)
        context = ExecutionContext()
        result = await refresh_knowledge_view(root, scope=before, context=context)
        assert result.status == "completed", result
        current = storage_scope(root, identity)
        assert set(current.knowledge.unit_revisions()) == {
            first.unit_revision_id,
            second.unit_revision_id,
        }
        for name in ("fixture", "second"):
            assert "17" in read_page(root, f"summaries/{name}.md", scope=current).body
        assert not refresh_status(root, scope=current)["needs_refresh"]
        assert len(service.requests) == 8
    from ultrakb.application.knowledge_tasks import read_knowledge_execution

    detail = read_knowledge_execution(root, context.storage_execution.execution_id)
    assert len(detail.publications) == 1
    assert detail.publications[0].publication_id == current.publication_id
    assert {request.unit_revision_id for request in detail.requests} == {
        first.unit_revision_id,
        second.unit_revision_id,
    }


@pytest.mark.asyncio
async def test_segmented_refresh_reuses_its_exact_ready_shared_index(tmp_path):
    from test_storage_indexing import indexing_response

    from ultrakb.config import save_config

    root, identity, unit = compile_input(
        tmp_path, segmented=True, body_text="# Fixture\nThe value is 17.\n"
    )
    save_config(root / ".ultrakb/config.yaml", {"model_policy": {"max_calls": 16, "retries": 0}})
    with StreamTransport(indexing_response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        before = await compiled(root, identity, unit)
        sent = len(service.requests)
        result = await refresh_knowledge_view(root, scope=before)
        assert result.status == "completed", result
        current = storage_scope(root, identity)
        assert current.knowledge.index_revisions() == before.knowledge.index_revisions()
        assert "17" in read_page(root, "summaries/fixture.md", scope=current).body
        assert len(service.requests) > sent
        refreshed_requests = json.dumps(service.requests[sent:])
        assert "detect if there is a table of content" not in refreshed_requests
        assert (
            "your task is to generate the tree structure of the document" not in refreshed_requests
        )


@pytest.mark.asyncio
async def test_refresh_reprepares_a_changed_projection_baseline_without_model_resends(
    tmp_path, monkeypatch
):
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, unit = compile_input(tmp_path)
    with StreamTransport([*responses(), *responses()]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as unsynced:

            def interrupted_sync(self, **kwargs):
                raise OSError("Initial projection remains pending")

            unsynced.setattr(ProjectionStore, "sync", interrupted_sync)
            before = await compiled(root, identity, unit)
        context = ExecutionContext()
        publish = KnowledgeStore.publish
        with monkeypatch.context() as concurrent:

            def advance_projection_before_publish(self, prepared, **kwargs):
                assert ProjectionStore(root, identity).sync().status == "synced"
                return publish(self, prepared, **kwargs)

            concurrent.setattr(KnowledgeStore, "publish", advance_projection_before_publish)
            interrupted = await refresh_knowledge_view(root, scope=before, context=context)
        assert interrupted.status in {"failed", "conflict"}, interrupted
        assert storage_scope(root, identity).publication_id == before.publication_id
        assert len(service.requests) == 4
        recovered = await refresh_knowledge_view(root, scope=before, context=context)
        assert recovered.status == "completed", recovered
        assert len(service.requests) == 4


@pytest.mark.asyncio
async def test_refresh_requires_review_before_replacing_manual_navigation(tmp_path):
    root, identity, unit = compile_input(tmp_path)
    with StreamTransport([*responses(), *responses()]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        before = await compiled(root, identity, unit)
        navigation = before.knowledge.read("index.md") + b"\nMANUAL_NAVIGATION_SENTINEL\n"
        store = KnowledgeStore(root, identity)
        store.publish(store.prepare_changes(before.knowledge, {"index.md": navigation}))
        manual = storage_scope(root, identity)
        result = await refresh_knowledge_view(root, scope=manual)
        assert result.status == "awaiting_confirmation", result
        assert storage_scope(root, identity).publication_id == manual.publication_id
        proposal = read_refresh_proposal(root, result.proposal_id, scope=manual)
        assert "index.md" in proposal["conflicts"]
        assert "MANUAL_NAVIGATION_SENTINEL" in proposal["diff"]
        accepted = accept_refresh_proposal(
            root, result.proposal_id, version=proposal["version"], scope=manual
        )
        assert accepted.status == "completed", accepted
        assert manual.knowledge.read("index.md") == navigation
        assert len(service.requests) == 4


@pytest.mark.asyncio
async def test_reviewed_refresh_reprepares_an_obsolete_sync_plan_without_new_model_work(
    tmp_path, monkeypatch
):
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, unit = compile_input(tmp_path)
    with StreamTransport([*responses(), *responses()]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as unsynced:

            def interrupted_sync(self, **kwargs):
                raise OSError("Worktree remains pending")

            unsynced.setattr(ProjectionStore, "sync", interrupted_sync)
            initial = await compiled(root, identity, unit)
            page = read_page(root, "summaries/fixture.md", scope=initial)
            saved = save_page(
                root,
                "summaries/fixture",
                "Manual explanation.",
                version=page.version,
                publication_id=initial.publication_id,
                scope=initial,
            )
            assert saved.status == "saved", saved
        manual = storage_scope(root, identity)
        context = ExecutionContext()
        first = await refresh_knowledge_view(root, scope=manual, context=context)
        assert first.status == "awaiting_confirmation", first
        original = read_refresh_proposal(root, first.proposal_id, scope=manual)
        assert ProjectionStore(root, identity).sync().status == "synced"
        denied = accept_refresh_proposal(
            root, first.proposal_id, version=original["version"], scope=manual
        )
        assert denied.status == "conflict", denied
        rebuilt = await refresh_knowledge_view(root, scope=manual, context=context)
        assert rebuilt.status == "awaiting_confirmation", rebuilt
        assert rebuilt.proposal_id != first.proposal_id
        reviewed = read_refresh_proposal(root, rebuilt.proposal_id, scope=manual)
        assert reviewed["diff"] == original["diff"]
        accepted = accept_refresh_proposal(
            root, rebuilt.proposal_id, version=reviewed["version"], scope=manual
        )
        assert accepted.status == "completed", accepted
        assert len(service.requests) == 4
