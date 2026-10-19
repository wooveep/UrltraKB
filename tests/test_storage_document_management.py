"""Management selects retained units and advances only its own accepted publications."""

import asyncio
import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.document_actions import DocumentIdentity, document_inventory
from ultrakb.application.document_batches import preview_document_batch, validate_document_batch
from ultrakb.application.knowledge_bases import get_kb_list
from ultrakb.application.recompilation import recompile_document, select_recompilation
from ultrakb.application.sources import source_inventory
from ultrakb.application.views import storage_scope
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore

pytestmark = pytest.mark.integration


def responses(count=1):
    return [
        item
        for _ in range(count)
        for item in (
            {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        )
    ]


def replace_target(root, identity, target, tmp_path):
    body = tmp_path / "replacement.md"
    body.write_text("The updated value is 29.")
    catalog = CatalogStore(root, identity)
    processing = catalog.retain_processing(
        target.source_revision_id,
        {"fixture.md": body},
        body_path="fixture.md",
        fingerprint="replacement-v2",
        operation_id="replacement-v2",
        decision=classify_markdown_tokens(5001),
    )
    return catalog.plan_unit(processing.processing_id, key=target.key, doc_name=target.doc_name)


def test_inventory_keeps_published_basis_separate_from_a_new_retained_target(tmp_path):
    root, identity, target = compile_input(tmp_path)
    with StreamTransport(responses()) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = asyncio.run(
            recompile_document(
                root, target.source_id, unit_id=target.unit_id, scope=storage_scope(root, identity)
            )
        )
        assert result.status == "compiled", result.message
    scope = storage_scope(root, identity)
    newer = replace_target(root, identity, target, tmp_path)
    [source] = source_inventory(root, scope=scope)
    [unit] = source["units"]
    assert unit["successful_revision_id"] == target.unit_revision_id
    assert unit["target_revision_id"] == newer.unit_revision_id
    assert unit["processing"]["length_class"] == "short"
    assert unit["target_processing"]["length_class"] == "long"
    assert source["validity"] == "needs_refresh"
    inventory = get_kb_list(root, scope=scope)
    assert inventory["publication_id"] == scope.publication_id
    assert inventory["summaries"] == [target.doc_name]
    [row] = document_inventory(root, scope=scope)
    assert row.actions["read"].allowed and row.actions["ask"].allowed
    assert row.actions["retry"].allowed and not row.actions["recompile"].allowed
    assert "知识待更新" in row.status
    assert not (root / ".ultrakb/catalog").exists()
    assert not (root / ".ultrakb/hashes.json").exists()


def test_confirmation_rejects_replaced_unit_before_model_send(tmp_path):
    root, identity, target = compile_input(tmp_path)
    scope = storage_scope(root, identity)
    preview = select_recompilation(root, target.source_id, confirmation=True, scope=scope)
    assert preview.status == "ready"
    assert [item.unit_id for item in preview.targets] == [target.unit_id]
    replace_target(root, identity, target, tmp_path)
    with StreamTransport([]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = asyncio.run(
            recompile_document(
                root,
                target.source_id,
                unit_id=target.unit_id,
                version=preview.version,
                scope=scope,
            )
        )
        assert result.status == "conflict"
        assert not service.requests


def test_selection_never_authorizes_a_target_accepted_while_rendering_the_preview(
    tmp_path, monkeypatch
):
    root, identity, target = compile_input(tmp_path)
    scope = storage_scope(root, identity)
    original, changed = CatalogStore.processing, False

    def read_then_replace(self, processing_id):
        nonlocal changed
        value = original(self, processing_id)
        if not changed:
            changed = True
            replace_target(root, identity, target, tmp_path)
        return value

    with monkeypatch.context() as concurrent:
        concurrent.setattr(CatalogStore, "processing", read_then_replace)
        preview = select_recompilation(root, target.source_id, confirmation=True, scope=scope)
    assert preview.status == "ready" and preview.targets[0].kind == "short"
    with StreamTransport([]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = asyncio.run(
            recompile_document(
                root, target.source_id, unit_id=target.unit_id, version=preview.version, scope=scope
            )
        )
        assert result.status == "conflict"
        assert not service.requests


@pytest.mark.asyncio
async def test_http_batch_compiles_two_retained_units_with_authoritative_usage(tmp_path):
    from ultrakb.api_recompile import iter_recompile

    root, identity, first = compile_input(tmp_path)
    catalog = CatalogStore(root, identity)
    second = catalog.plan_unit(first.processing_id, key="second", doc_name="second")
    with StreamTransport(responses(2)) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        events = [
            event
            async for event in iter_recompile(
                root, all_docs=True, scope=storage_scope(root, identity)
            )
        ]
        final = events[-1]
        assert final["recompiled"] == 2, events
        assert len(service.requests) == 4
    assert final["model_usage"]["current"]["actual_sends"] == 4
    assert final["model_usage"]["cumulative"]["actual_sends"] == 4
    assert {
        unit.unit_revision_id for unit in storage_scope(root, identity).knowledge.input_units()
    } == {
        first.unit_revision_id,
        second.unit_revision_id,
    }


def test_native_batch_retains_all_worksheet_identities_and_revalidates_targets(tmp_path):
    from ultrakb.desktop.document_batch_actions import requests_for

    root, identity, first = compile_input(tmp_path)
    second = CatalogStore(root, identity).plan_unit(
        first.processing_id, key="second", doc_name="second"
    )
    with StreamTransport(responses(2)) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        for target in (first, second):
            result = asyncio.run(
                recompile_document(
                    root,
                    first.source_id,
                    unit_id=target.unit_id,
                    scope=storage_scope(root, identity),
                )
            )
            assert result.status == "compiled", result.message
    scope = storage_scope(root, identity)
    selected = [DocumentIdentity("source", first.source_id)]
    preview = preview_document_batch(root, selected, scope=scope)
    assert preview.approved == tuple(selected) and len(preview.targets) == 2
    requests = requests_for(preview, scope.view_id)
    assert {request.unit_id for request in requests} == {first.unit_id, second.unit_id}
    replace_target(root, identity, first, tmp_path)
    from ultrakb.application.document_batches import DocumentBatchChanged

    with pytest.raises(DocumentBatchChanged):
        validate_document_batch(preview, scope=scope)


@pytest.mark.asyncio
async def test_batch_does_not_adopt_a_foreign_publication_between_units(tmp_path):
    from ultrakb.api_recompile import iter_recompile
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, first = compile_input(tmp_path)
    CatalogStore(root, identity).plan_unit(first.processing_id, key="second", doc_name="second")
    store = KnowledgeStore(root, identity)
    with StreamTransport(responses()) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        events = []
        foreign = None
        async for event in iter_recompile(root, all_docs=True, scope=storage_scope(root, identity)):
            events.append(event)
            if event["event"] == "doc" and event["status"] == "ok":
                foreign = store.publish(
                    store.prepare_changes(store.bind(), {"index.md": b"Foreign edit"})
                )
        assert len(service.requests) == 2
    assert events[-1]["status"] == "partial" and events[-1]["recompiled"] == 1
    assert events[-1]["docs"][-1]["status"] == "conflict"
    assert store.bind().publication_id == foreign.publication_id
    assert events[-1]["model_usage"]["current"]["actual_sends"] == 2


def test_merging_unit_receipts_preserves_their_scope_and_deduplicates_parent_cost(tmp_path):
    from openpyxl import Workbook
    from test_storage_questions import question_knowledge

    from ultrakb.application.documents import import_document
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_usage import merge_usage_receipts
    from ultrakb.storage.usage_store import UsageStore

    root, identity, _ = question_knowledge(tmp_path)
    book = Workbook()
    book.active["A1"] = "First"
    book.create_sheet("Second")["A1"] = "Second"
    source = tmp_path / "book.xlsx"
    book.save(source)
    context = ExecutionContext(storage_identity=identity)
    with StreamTransport(responses(2)) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        imported = import_document(
            root, source, context=context, scope=storage_scope(root, identity)
        )
        assert imported.status == "added", imported.message
        assert len(service.requests) == 4
    [one, two] = [unit.model_usage for unit in imported.units]
    for receipts, count in (
        ([one], 2),
        ([one, one], 2),
        ([one, two], 4),
        ([imported.model_usage, one], 4),
    ):
        merged = merge_usage_receipts(root, receipts)
        assert merged["current"]["actual_sends"] == count
        assert merged["cumulative"]["actual_sends"] == count
    whole = UsageStore(root, identity).receipt(execution_id=context.storage_execution.execution_id)
    combined = merge_usage_receipts(root, [whole, one])
    assert combined["current"]["actual_sends"] == combined["cumulative"]["actual_sends"] == 4


@pytest.mark.parametrize("refresh", [False, True])
def test_cli_development_binding_compiles_each_unit_and_keeps_the_original_batch_confirmation(
    tmp_path, monkeypatch, refresh
):
    from click.testing import CliRunner

    import ultrakb.cli as cli_module

    root, identity, first = compile_input(tmp_path)
    if refresh:
        from ultrakb.storage.knowledge_store import KnowledgeStore

        store = KnowledgeStore(root, identity)
        store.publish(store.prepare_changes(store.bind(), {"AGENTS.md": b"Previous schema"}))
    CatalogStore(root, identity).plan_unit(first.processing_id, key="second", doc_name="second")
    monkeypatch.setattr(cli_module, "_find_kb_dir", lambda *args: root)
    monkeypatch.setattr(cli_module, "_selected_scope", lambda *args: storage_scope(root, identity))
    with StreamTransport(responses(2)) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = CliRunner().invoke(
            cli_module.cli,
            ["recompile", "--all", "--yes"] + (["--refresh-schema"] if refresh else []),
        )
        assert result.exit_code == 0, result.output
        assert "recompiled 2, skipped 0" in result.output
        assert len(service.requests) == 4


def test_schema_refresh_publishes_the_fixed_schema_and_keeps_the_previous_version(tmp_path):
    from ultrakb.application.recompilation import refresh_schema
    from ultrakb.schema import AGENTS_MD
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, _ = compile_input(tmp_path)
    store = KnowledgeStore(root, identity)
    store.publish(store.prepare_changes(store.bind(), {"AGENTS.md": b"Previous schema"}))
    old = storage_scope(root, identity)
    assert refresh_schema(root, scope=old)
    bound = store.bind()
    assert bound.read("AGENTS.md").decode() == AGENTS_MD
    assert bound.read("AGENTS.md.bak") == b"Previous schema"
    assert old.knowledge.read("AGENTS.md") == b"Previous schema"
    assert not refresh_schema(root, scope=storage_scope(root, identity))


def test_usage_union_uses_one_read_snapshot_while_late_receipts_arrive(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_usage import merge_usage_receipts
    from ultrakb.storage import usage_selection
    from ultrakb.storage.usage_store import UsageStore

    root, identity, target = compile_input(tmp_path)
    context = ExecutionContext()
    with StreamTransport(responses()) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = asyncio.run(
            recompile_document(
                root,
                target.source_id,
                unit_id=target.unit_id,
                context=context,
                scope=storage_scope(root, identity),
            )
        )
        assert result.status == "compiled", result.message
    store = UsageStore(root, identity)
    request = store.requests(execution_id=context.storage_execution.execution_id).records[0]
    whole = store.receipt(execution_id=context.storage_execution.execution_id)
    stage, delivered = usage_selection.storage_stage, []

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            cursor = self.connection.execute(sql, parameters)
            if "sum(" in sql and parameters and parameters[0] == "unit" and not delivered:
                rows = cursor.fetchall()
                store.merge(request.request_attempt_id, version=1000000, input_total=10000)
                delivered.append(True)

                class Rows:
                    def fetchall(self):
                        return rows

                return Rows()
            return cursor

    @contextmanager
    def late_receipt_stage(*args, **options):
        with stage(*args, **options) as db:
            db.connection = Connection(db.connection)
            yield db

    with monkeypatch.context() as late:
        late.setattr(usage_selection, "storage_stage", late_receipt_stage)
        merged = merge_usage_receipts(root, [whole, result.units[0].model_usage])
    assert delivered
    assert merged["current"]["input_total"] == merged["cumulative"]["input_total"] == 240
    refreshed = merge_usage_receipts(root, [whole, result.units[0].model_usage])
    assert refreshed["current"]["input_total"] == refreshed["cumulative"]["input_total"] == 10120


def test_old_usage_receipt_cannot_be_recomputed_from_a_new_database_at_the_same_path(tmp_path):
    from ultrakb.llm_usage import merge_usage_receipts
    from ultrakb.storage.database import create_database

    root, identity, target = compile_input(tmp_path)
    with StreamTransport(responses()) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = asyncio.run(
            recompile_document(
                root, target.source_id, unit_id=target.unit_id, scope=storage_scope(root, identity)
            )
        )
        assert result.status == "compiled", result.message
    root.rename(tmp_path / "previous-kb")
    replacement = create_database(root)
    assert replacement != identity
    with pytest.raises(ValueError):
        merge_usage_receipts(root, [result.model_usage])
    assert result.model_usage["current"]["actual_sends"] == 2


@pytest.mark.asyncio
async def test_batch_final_keeps_compiled_outcomes_when_usage_confirmation_fails(
    tmp_path, monkeypatch
):
    from ultrakb.api_recompile import iter_recompile
    from ultrakb.storage.usage_store import UsageStore

    root, identity, target = compile_input(tmp_path)

    def unreadable(*args):
        raise OSError("Usage query interrupted after publication")

    monkeypatch.setattr(UsageStore, "combined_receipt", unreadable)
    with StreamTransport(responses()) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        events = [
            event
            async for event in iter_recompile(
                root, target.source_id, scope=storage_scope(root, identity)
            )
        ]
        assert len(service.requests) == 2
    final = events[-1]
    assert final["recompiled"] == 1 and final["docs"][0]["status"] == "ok"
    assert final["model_usage"] is None and "confirm usage" in final["unfinished"]
    assert storage_scope(root, identity).knowledge.unit_revisions() == (target.unit_revision_id,)


@pytest.mark.asyncio
async def test_batch_keeps_a_compiled_unit_if_the_next_publication_cannot_be_confirmed(
    tmp_path, monkeypatch
):
    from ultrakb.api_recompile import iter_recompile
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, target = compile_input(tmp_path)
    CatalogStore(root, identity).plan_unit(target.processing_id, key="second", doc_name="second")
    scope = storage_scope(root, identity)
    original = KnowledgeStore.bind

    def unreadable(self, publication_id=None, **options):
        value = original(self, publication_id, **options)
        if publication_id is not None and publication_id != scope.publication_id:
            raise OSError("Read confirmation interrupted after publication")
        return value

    monkeypatch.setattr(KnowledgeStore, "bind", unreadable)
    with StreamTransport(responses()) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        events = [event async for event in iter_recompile(root, all_docs=True, scope=scope)]
        assert len(service.requests) == 2
    final = events[-1]
    assert final["status"] == "partial" and final["recompiled"] == 1
    assert final["remaining"] == 1 and final["docs"][0]["status"] == "ok"
    assert "confirm next publication" in final["unfinished"]
