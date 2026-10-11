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


def _import_rule(kb_dir, monkeypatch, name, fact):
    import pymupdf

    from openkb.application.documents import import_document

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
    )
    assert result.status == "added"
    return result


@pytest.fixture
def independent_documents(kb_dir, monkeypatch):
    return [
        _import_rule(kb_dir, monkeypatch, f"manual-{number}", fact)
        for number, fact in (("1", "TLS is required."), ("2", "TLS is forbidden."))
    ]


def test_query_pins_a_revision_and_reports_later_knowledge_changes(kb_dir, independent_documents):
    from openkb.application.pages import read_page, save_page
    from openkb.application.query_views import (
        read_query_page,
        resolve_query_views,
        selection_current,
    )
    from openkb.application.views import view_scope

    first, _ = independent_documents
    scope = view_scope(kb_dir, first.units[0].view_id)
    selection = resolve_query_views(kb_dir, "What is the TLS rule?", scope=scope)
    page = read_page(kb_dir, "summaries/manual-1", scope=scope)
    save_page(kb_dir, page.path, "A later manual correction.", version=page.version, scope=scope)
    assert "TLS is required." in read_query_page(
        selection, "summaries/manual-1.md", view_id=scope.view_id
    )
    assert not selection_current(selection)
