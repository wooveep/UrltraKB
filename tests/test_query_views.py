"""Snapshot selection pins the actual readable source bytes for one question."""

import json
from types import SimpleNamespace

import pytest


async def _chat_chunks(delta, finish):
    from openai.types.chat import ChatCompletionChunk

    for data, reason in (({"role": "assistant", **delta}, None), ({}, finish)):
        yield ChatCompletionChunk(
            id="answer",
            created=0,
            model="test",
            object="chat.completion.chunk",
            choices=[{"index": 0, "delta": data, "finish_reason": reason}],
        )


def _import_rule(
    kb_dir,
    monkeypatch,
    name,
    version,
    fact,
    family="installation",
    *,
    product="WinStack",
    product_id=None,
):
    import pymupdf

    from openkb.application.documents import import_document
    from openkb.view_records import SourceMetadata

    path = kb_dir / f"{name}.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), fact)
        pdf.save(path)
    replies = iter([{"description": "Connection rule", "content": fact}, {}])
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(replies))))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    result = import_document(
        kb_dir,
        path,
        metadata=SourceMetadata(
            product=product,
            applicable_versions=(version,),
            family=family,
            **({"product_id": product_id} if product_id else {}),
        ),
    )
    assert result.status == "added"
    return result


def _legacy_product(kb_dir, name):
    """A pre-fix duplicate catalog, which new normalized imports cannot create."""
    import uuid

    from openkb.locks import kb_ingest_lock
    from openkb.mutation import mutation_scope
    from openkb.source_catalog import record_path, write_record
    from openkb.view_records import Product

    product = Product(product_id=uuid.uuid4().hex, name=name)
    path = record_path(kb_dir, "products", product.product_id)
    with (
        kb_ingest_lock(kb_dir / ".openkb"),
        mutation_scope(kb_dir, [path], operation="fixture-legacy-product"),
    ):
        write_record(path, product)
    return product.product_id


@pytest.mark.parametrize(
    "product", ["CNware-WinStack", "CNware WinStack 虚拟化云平台", "CNWARE WINSTACK"]
)
def test_explicit_scope_keeps_selected_product_evidence(kb_dir, monkeypatch, product):
    from openkb.application.query_views import read_query_page, resolve_query_views
    from openkb.application.views import view_scope

    chosen = _import_rule(kb_dir, monkeypatch, "chosen", "9.4.0", "K3s recovery.", product=product)
    scope = view_scope(kb_dir, chosen.units[0].view_id)
    before = resolve_query_views(kb_dir, "CNware WinStack V9.4.0 recovery?", scope=scope)
    _import_rule(
        kb_dir, monkeypatch, "other", "9.4.0", "Other instructions.", product="CNware WinStack"
    )
    after = resolve_query_views(kb_dir, "CNware WinStack V9.4.0 recovery?", scope=scope)
    assert [view.view_id for view in before.views] == [scope.view_id]
    assert [view.view_id for view in after.views] == [scope.view_id]
    assert "K3s recovery." in read_query_page(after, "sources/chosen.md", view_id=scope.view_id)
    assert chosen.source_revision_id in after.views[0].source_revision_ids
    assert after.missing == ()


@pytest.fixture
def opposing_versions(kb_dir, monkeypatch):
    return [
        _import_rule(kb_dir, monkeypatch, f"manual-{version}", version, fact)
        for version, fact in (("1", "TLS is required."), ("2", "TLS is forbidden."))
    ]


def test_query_pins_a_revision_and_reports_later_knowledge_changes(kb_dir, opposing_versions):
    from openkb.application.pages import read_page, save_page
    from openkb.application.query_views import (
        read_query_page,
        resolve_query_views,
        selection_current,
    )
    from openkb.application.views import view_scope

    first, _ = opposing_versions
    scope = view_scope(kb_dir, first.units[0].view_id)
    selection = resolve_query_views(kb_dir, "What is the TLS rule?", scope=scope)
    page = read_page(kb_dir, "summaries/manual-1", scope=scope)
    save_page(kb_dir, page.path, "A later manual correction.", version=page.version, scope=scope)
    assert "TLS is required." in read_query_page(
        selection, "summaries/manual-1.md", view_id=scope.view_id
    )
    assert not selection_current(selection)
