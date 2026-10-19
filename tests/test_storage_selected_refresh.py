"""Selected refresh uses accepted originals and never reruns document compilation."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.refresh import refresh_selected_sources, refresh_status
from ultrakb.application.views import storage_scope
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def prepared_selection(tmp_path):
    root, identity, first = compile_input(tmp_path)
    catalog = CatalogStore(root, identity)
    other = tmp_path / "second.txt"
    other.write_text("The second value is 31.")
    admitted = catalog.admit(other, source_key="second")
    processed = catalog.retain_processing(
        admitted.revision_id,
        {"second.md": other},
        body_path="second.md",
        fingerprint="second",
        operation_id="second",
        decision=classify_markdown_tokens(8),
    )
    second = catalog.plan_unit(processed.processing_id, key="body", doc_name="second")
    store = KnowledgeStore(root, identity)
    files = {
        "index.md": b"Fixture",
        "summaries/fixture.md": (
            b"---\ntype: Summary\nfull_text: sources/fixture.md\nsources: [sources/fixture.md]\n"
            b"---\nThe value is 17."
        ),
        "summaries/second.md": (
            b"---\ntype: Summary\nfull_text: sources/second.md\nsources: [sources/second.md]\n"
            b"---\nThe second value is 31."
        ),
        "concepts/value.md": (
            b"---\ndescription: Recorded values\n"
            b"sources: [summaries/fixture.md, summaries/second.md]\n"
            b"---\n# Value\n\nThe value is 17.\n\nThe second value is 31.\n"
        ),
    }
    store.publish(
        store.prepare_revision(
            files,
            expected=store.bind().publication_id,
            unit_revisions=(first.unit_revision_id, second.unit_revision_id),
            page_inputs={
                "index.md": (first.unit_revision_id, second.unit_revision_id),
                "summaries/fixture.md": (first.unit_revision_id,),
                "summaries/second.md": (second.unit_revision_id,),
                "concepts/value.md": (first.unit_revision_id, second.unit_revision_id),
            },
            generated_paths=tuple(files),
        )
    )
    changed = tmp_path / "updated.txt"
    changed.write_text("The value is 23.")
    revision = catalog.admit(changed, source_key="fixture", expected=first.source_revision_id)
    processing = catalog.retain_processing(
        revision.revision_id,
        {"fixture.md": changed},
        body_path="fixture.md",
        fingerprint="updated",
        operation_id="updated",
        decision=classify_markdown_tokens(8),
    )
    target = catalog.plan_unit(processing.processing_id, key="body", doc_name="fixture")
    return root, identity, target, second


def respond(request, index):
    for message in request["messages"]:
        text = message.get("content")
        if isinstance(text, str) and text.startswith("Temporary editable page blocks:\n"):
            value, _ = json.JSONDecoder().raw_decode(text.split("\n", 1)[1])
            return {
                "content": json.dumps(
                    {
                        "status": "updated",
                        "base_sha256": value["base_sha256"],
                        "edits": [
                            {
                                "block_id": block["id"],
                                "operation": "replace",
                                "content": block["text"].replace("17.", "23."),
                            }
                            for block in value["blocks"]
                            if "17." in block["text"]
                        ],
                    }
                )
            }
    return {
        "content": json.dumps(
            {"description": "Fixture", "content": "The value is 23."}
            if index == 0
            else {"concepts": [], "entities": []}
        )
    }


@pytest.mark.asyncio
async def test_selected_summary_reuses_accepted_bytes_and_keeps_other_pending_pages(tmp_path):
    root, identity, target, _ = prepared_selection(tmp_path)
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        compiled = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, scope=storage_scope(root, identity)
        )
        assert compiled.status == "compiled", compiled
        scope = storage_scope(root, identity)
        before = scope.knowledge.read("summaries/fixture.md")
        (root / ".ultrakb/config.yaml").unlink()
        result = await refresh_selected_sources(
            root, [target.source_id], scope=scope, page_paths=["summaries/fixture.md"]
        )
        assert result.status == "partial", result
        current = storage_scope(root, identity)
        assert current.knowledge.read("summaries/fixture.md") == before
        pending = refresh_status(root, scope=current)["needs_refresh"]
        assert "summaries/fixture.md" not in pending and "concepts/value.md" in pending
        assert len(service.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [None, "accepted", "publication"])
@pytest.mark.parametrize("pages", [None, ["summaries/fixture.md", "concepts/value.md"]])
async def test_selected_shared_page_uses_complete_originals_and_recovers_paid_work(
    tmp_path, monkeypatch, interrupt, pages
):
    from ultrakb.storage.checkpoint_store import CompileWork

    root, identity, target, second = prepared_selection(tmp_path)
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        assert (
            await recompile_document(
                root, target.source_id, unit_id=target.unit_id, scope=storage_scope(root, identity)
            )
        ).status == "compiled"
        scope, context = storage_scope(root, identity), ExecutionContext()
        normal = scope.knowledge.read("summaries/second.md")
        if interrupt:
            save = CompileWork.save

            def lost_step(self, key, *args, **kwargs):
                result = save(self, key, *args, **kwargs)
                if key.startswith("page:"):
                    raise OSError("Accepted page delivery interrupted")
                return result

            def lost_publication(*args, **kwargs):
                raise OSError("Publication unavailable")

            with monkeypatch.context() as failure:
                if interrupt == "accepted":
                    failure.setattr(CompileWork, "save", lost_step)
                else:
                    failure.setattr(KnowledgeStore, "publish", lost_publication)
                failed = await refresh_selected_sources(
                    root, [target.source_id], scope=scope, context=context, page_paths=pages
                )
            assert failed.status == "failed", failed
            assert storage_scope(root, identity).publication_id == scope.publication_id
        result = await refresh_selected_sources(
            root,
            [target.source_id],
            scope=scope,
            context=context,
            page_paths=list(reversed(pages)) if pages is not None else None,
        )
        assert result.status == ("partial" if pages is not None else "completed"), result
        current = storage_scope(root, identity)
        assert current.knowledge.unit_revisions() == scope.knowledge.unit_revisions()
        assert current.knowledge.read("summaries/second.md") == normal
        assert "23." in current.knowledge.read("concepts/value.md").decode()
        assert "31." in current.knowledge.read("concepts/value.md").decode()
        assert set(refresh_status(root, scope=current)["needs_refresh"]) == (
            {"index.md"} if pages is not None else set()
        )
        assert len(service.requests) == 3
        prompt = json.dumps(service.requests[-1])
        assert "The value is 23." in prompt and "The second value is 31." in prompt
        assert second.unit_revision_id in current.knowledge.unit_revisions()
        if interrupt is None:
            from ultrakb.storage.usage_store import UsageStore

            [request] = (
                UsageStore(root, identity)
                .requests(execution_id=context.storage_execution.execution_id)
                .records
            )
            assert request.source_id == target.source_id
            assert request.unit_id == target.unit_id
            assert request.unit_revision_id == target.unit_revision_id


@pytest.mark.asyncio
@pytest.mark.parametrize("late_change", [False, True])
async def test_selected_manual_page_preserves_annotations_and_requires_exact_review(
    tmp_path, late_change
):
    from ultrakb.application.pages import read_page, save_page
    from ultrakb.application.refresh import accept_refresh_proposal, read_refresh_proposal

    root, identity, target, _ = prepared_selection(tmp_path)
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        assert (
            await recompile_document(
                root, target.source_id, unit_id=target.unit_id, scope=storage_scope(root, identity)
            )
        ).status == "compiled"
        scope = storage_scope(root, identity)
        page = read_page(root, "concepts/value.md", scope=scope)
        assert (
            save_page(
                root,
                page.path,
                page.body + "\nHuman review note.\n",
                version=page.version,
                scope=scope,
            ).status
            == "saved"
        )
        scope = storage_scope(root, identity)
        result = await refresh_selected_sources(root, [target.source_id], scope=scope)
        assert result.status == "awaiting_confirmation", result
        proposal = read_refresh_proposal(root, result.proposal_id, scope=scope)
        assert "23." in proposal["diff"]
        if late_change:
            source = tmp_path / "late.txt"
            source.write_text("A late replacement")
            CatalogStore(root, identity).admit(
                source, source_key="fixture", expected=target.source_revision_id
            )
        accepted = accept_refresh_proposal(
            root, result.proposal_id, version=proposal["version"], scope=scope
        )
        assert accepted.status == ("conflict" if late_change else "completed"), accepted
        current = storage_scope(root, identity)
        assert "Human review note." in current.knowledge.read("concepts/value.md").decode()
        assert len(service.requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_association", [False, True])
async def test_selected_refresh_does_not_use_an_unselected_new_original(
    tmp_path, manual_association
):
    root, identity, target, second = prepared_selection(tmp_path)

    def changing_response(request, index):
        if index == 2:
            return {
                "content": json.dumps(
                    {"description": "Second", "content": "The second value is 37."}
                )
            }
        if index == 3:
            return {"content": json.dumps({"concepts": [], "entities": []})}
        return respond(request, index)

    with StreamTransport(changing_response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        assert (
            await recompile_document(
                root, target.source_id, unit_id=target.unit_id, scope=storage_scope(root, identity)
            )
        ).status == "compiled"
        catalog = CatalogStore(root, identity)
        source = tmp_path / "second-new.txt"
        source.write_text("The second value is 37.")
        revision = catalog.admit(source, source_key="second", expected=second.source_revision_id)
        processing = catalog.retain_processing(
            revision.revision_id,
            {"second.md": source},
            body_path="second.md",
            fingerprint="second2",
            operation_id="second2",
            decision=classify_markdown_tokens(8),
        )
        other = catalog.plan_unit(processing.processing_id, key="body", doc_name="second")
        assert (
            await recompile_document(
                root, other.source_id, unit_id=other.unit_id, scope=storage_scope(root, identity)
            )
        ).status == "compiled"
        scope = storage_scope(root, identity)
        if manual_association:
            store = KnowledgeStore(root, identity)
            content = scope.knowledge.read("concepts/value.md")
            store.publish(
                store.prepare_changes(
                    scope.knowledge,
                    {
                        "concepts/value.md": content.replace(
                            b"summaries/fixture.md, summaries/second.md", b"summaries/fixture.md"
                        )
                    },
                )
            )
            scope = storage_scope(root, identity)
        before = scope.knowledge.read("concepts/value.md")
        result = await refresh_selected_sources(root, [target.source_id], scope=scope)
        assert result.status == "partial", result
        current = storage_scope(root, identity)
        assert current.knowledge.read("concepts/value.md") == before
        reasons = refresh_status(root, scope=current)["needs_refresh"]["concepts/value.md"]
        assert {r["source_id"] for r in reasons} == {target.source_id, second.source_id}
        assert len(service.requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["source", "publication"])
async def test_selected_refresh_rechecks_its_fixed_inputs_after_model_wait(tmp_path, changed):
    root, identity, target, _ = prepared_selection(tmp_path)
    changed_head = []

    def concurrent_response(request, index):
        if index == 2:
            if changed == "source":
                source = tmp_path / "later.txt"
                source.write_text("The value is now 29.")
                CatalogStore(root, identity).admit(
                    source, source_key="fixture", expected=target.source_revision_id
                )
            else:
                store = KnowledgeStore(root, identity)
                store.publish(store.prepare_changes(store.bind(), {"reports/later.md": b"Later"}))
            changed_head.append(storage_scope(root, identity).publication_id)
        return respond(request, index)

    with StreamTransport(concurrent_response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        assert (
            await recompile_document(
                root, target.source_id, unit_id=target.unit_id, scope=storage_scope(root, identity)
            )
        ).status == "compiled"
        result = await refresh_selected_sources(
            root, [target.source_id], scope=storage_scope(root, identity)
        )
        assert result.status == "conflict", result
        assert storage_scope(root, identity).publication_id == changed_head[0]
        assert len(service.requests) == 3


@pytest.mark.parametrize("adapter", ["cli", "http", "native"])
def test_selected_summary_has_the_same_partial_outcome_in_each_adapter(
    tmp_path, monkeypatch, adapter
):
    import asyncio

    root, identity, target, _ = prepared_selection(tmp_path)
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = asyncio.run(
            recompile_document(
                root, target.source_id, unit_id=target.unit_id, scope=storage_scope(root, identity)
            )
        )
        assert result.status == "compiled", result
        (root / ".ultrakb/config.yaml").unlink()
        if adapter == "cli":
            from click.testing import CliRunner

            import ultrakb.cli as cli

            monkeypatch.setattr(cli, "_find_kb_dir", lambda *args: root)
            monkeypatch.setattr(cli, "_selected_scope", lambda *args: storage_scope(root, identity))
            result = CliRunner().invoke(
                cli.cli,
                ["refresh", "run", "--source", target.source_id, "--page", "summaries/fixture.md"],
            )
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
            assert payload["status"] == "partial", payload
        elif adapter == "http":
            from fastapi.testclient import TestClient

            import ultrakb.api as api
            import ultrakb.api_refresh as api_refresh
            import ultrakb.api_views as api_views

            monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
            monkeypatch.setattr(api_refresh, "_resolve_kb", lambda *args: root)
            monkeypatch.setattr(
                api_views, "view_scope", lambda *args, **kwargs: storage_scope(root, identity)
            )
            with TestClient(api.create_app()) as client:
                result = client.post(
                    "/api/v1/refresh",
                    json={
                        "kb": "fixture",
                        "source_ids": [target.source_id],
                        "page_paths": ["summaries/fixture.md"],
                    },
                )
                assert result.status_code == 200, result.text
                assert result.json()["status"] == "partial", result.text
        else:
            from uuid import uuid4

            import ultrakb.application.views as views
            from ultrakb.runtime.records import UnitIdentity
            from ultrakb.runtime.requests import RefreshKnowledge
            from ultrakb.runtime.worker import _execute

            monkeypatch.setattr(views, "view_scope", lambda *args: storage_scope(root, identity))
            request = RefreshKnowledge(
                source_ids=(target.source_id,),
                page_paths=("summaries/fixture.md",),
                view_id="legacy",
            )
            worker = UnitIdentity.create(
                uuid4().hex, 0, str(root), request, generation=identity.generation
            )
            result = _execute(request, worker, ExecutionContext())
            assert result.status == "partial", result
            assert result.resources == (str(root / "wiki"),)
            assert result.revision == storage_scope(root, identity).publication_id
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_unaccepted_unit_of_a_selected_source_stays_pending_before_paid_coordination(
    tmp_path,
):
    root, identity, target, _ = prepared_selection(tmp_path)
    catalog, store = CatalogStore(root, identity), KnowledgeStore(root, identity)
    extra = catalog.plan_unit(target.processing_id, key="extra", doc_name="extra")
    base = store.bind()
    basis = base.page_input_revisions()
    basis["concepts/value.md"] = (*basis["concepts/value.md"], extra.unit_revision_id)
    basis["index.md"] = (*basis["index.md"], extra.unit_revision_id)
    store.publish(
        store._prepare_revision(
            {},
            expected=base.publication_id,
            view_id="legacy",
            base=base,
            page_inputs=basis,
            unit_revisions=(*base.unit_revisions(), extra.unit_revision_id),
            index_revisions=base.index_revisions(),
        )
    )
    changed = tmp_path / "extra-new.md"
    changed.write_text("A changed extra unit from this source.")
    processing = catalog.retain_processing(
        target.source_revision_id,
        {"extra.md": changed},
        body_path="extra.md",
        fingerprint="extra-new",
        operation_id="extra-new",
        decision=classify_markdown_tokens(8),
    )
    catalog.plan_unit(processing.processing_id, key="extra", doc_name="extra")
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        assert (
            await recompile_document(
                root, target.source_id, unit_id=target.unit_id, scope=storage_scope(root, identity)
            )
        ).status == "compiled"
        result = await refresh_selected_sources(
            root, [target.source_id], scope=storage_scope(root, identity)
        )
        assert result.status == "partial", result
        pending = refresh_status(root, scope=storage_scope(root, identity))["needs_refresh"]
        assert any(reason["unit_id"] == extra.unit_id for reason in pending["concepts/value.md"])
        assert any(reason["unit_id"] == extra.unit_id for reason in pending["index.md"])
        assert len(service.requests) == 2
