"""Selected refresh uses real SDK requests and atomic publication on disposable KBs."""

import asyncio
import json

import pytest
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


def _reply(service, content):
    service.replies.append((200, response({"role": "assistant", "content": json.dumps(content)})))


def _import(kb, service, name, text, *, shared=False):
    from openkb.application.documents import import_document

    path = kb / f"{name}.md"
    path.write_text(text)
    _reply(service, {"description": name, "content": text})
    _reply(
        service, {"concepts": {"create": [{"name": "shared", "title": "Shared"}]}} if shared else {}
    )
    if shared:
        _reply(service, {"description": "Shared policies", "content": "# Shared\n\n" + text})
    result = import_document(kb, path)
    assert result.status == "added", result.message
    return result


def _prepared(kb, service):
    from openkb.application.views import view_scope

    (kb / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={service.url}\n")
    (kb / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  retries: 0\n  max_calls: 20\n"
    )
    first = _import(kb, service, "first", "The first policy permits direct use.")
    second = _import(kb, service, "second", "The second policy forbids direct use.", shared=True)
    _import(kb, service, "normal", "An independent policy remains available.")
    _import(kb, service, "first", "The first policy requires review.")
    _import(kb, service, "second", "The second policy permits approved use.")
    return [first.source_id, second.source_id], view_scope(kb, first.units[0].view_id)


def _editing_request(request):
    marker = "Temporary editable page blocks:\n"
    text = next(
        m["content"]
        for m in request["messages"]
        if isinstance(m["content"], str) and m["content"].startswith(marker)
    )
    return json.JSONDecoder().raw_decode(text[len(marker) :])[0]


def test_selected_multi_source_page_reads_all_originals_and_preserves_normal_documents(
    kb_dir, model_service, monkeypatch
):
    from openkb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    normal = scope.wiki_dir / "summaries/normal.md"
    before = normal.read_bytes()
    count = len(model_service.requests)

    def unchanged(request):
        edit = _editing_request(request)
        return response(
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "status": "unchanged",
                        "base_sha256": edit["base_sha256"],
                        "reason": "No additional shared contribution is required.",
                    }
                ),
            }
        )

    model_service.replies.append((200, unchanged))

    def forbidden(*args, **kwargs):
        raise AssertionError("Selected refresh must not run a document compiler")

    monkeypatch.setattr("openkb.agent.compiler.compile_short_doc", forbidden)
    monkeypatch.setattr("openkb.agent.compiler.compile_long_doc", forbidden)
    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "completed", result.message
    requests = model_service.requests[count:]
    assert len(requests) == 1
    text = json.dumps(requests[0]["messages"])
    assert "The first policy requires review." in text
    assert "The second policy permits approved use." in text
    assert "An independent policy remains available." not in text
    assert normal.read_bytes() == before
    assert not refresh_status(kb_dir, scope=scope)["needs_refresh"]


def test_selected_refresh_failure_retains_live_pages_and_pending_reasons(kb_dir, model_service):
    from openkb.application.refresh import refresh_selected_sources, refresh_status
    from openkb.unit_publication import wiki_versions

    sources, scope = _prepared(kb_dir, model_service)
    before = refresh_status(kb_dir, scope=scope)
    pages = wiki_versions(kb_dir, scope.wiki_dir)
    model_service.replies.extend([(400, {"error": {"message": "fixture refusal"}})] * 4)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "failed"
    assert refresh_status(kb_dir, scope=scope) == before
    assert wiki_versions(kb_dir, scope.wiki_dir) == pages


def test_selected_refresh_never_clears_an_unselected_source_reason(kb_dir, model_service):
    from openkb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    before = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    count = len(model_service.requests)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources[:1], scope=scope))

    assert result.status == "partial", result.message
    after = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    assert after["concepts/shared.md"] == before["concepts/shared.md"]
    assert any(
        reason["source_id"] == sources[1] for reasons in after.values() for reason in reasons
    )
    assert len(model_service.requests) == count


def _changed_reply(request):
    edit = _editing_request(request)
    return response(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "status": "updated",
                    "base_sha256": edit["base_sha256"],
                    "edits": [
                        {
                            "block_id": edit["blocks"][1]["id"],
                            "operation": "replace",
                            "content": "The first policy requires review.\n\n"
                            "The second policy permits approved use.\n\n",
                        }
                    ],
                }
            ),
        }
    )


def test_selected_refresh_publishes_both_source_changes_together(kb_dir, model_service):
    from openkb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    model_service.replies.append((200, _changed_reply))

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "completed", result.message
    text = (scope.wiki_dir / "concepts/shared.md").read_text()
    assert "The first policy requires review." in text
    assert "The second policy permits approved use." in text
    assert "The second policy forbids direct use." not in text
    assert not refresh_status(kb_dir, scope=scope)["needs_refresh"]


def test_selected_refresh_can_finish_pages_while_an_unrelated_source_is_incomplete(
    kb_dir, model_service
):
    from openkb.application.documents import import_document
    from openkb.application.query_views import read_query_page, resolve_query_views
    from openkb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    normal_summary = (scope.wiki_dir / "summaries/normal.md").read_bytes()
    (kb_dir / "normal.md").write_text("An unrelated new policy requires review.")
    _reply(model_service, {})
    _reply(model_service, {})
    failed = import_document(kb_dir, kb_dir / "normal.md")
    assert failed.status == "failed"
    pending = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    count = len(model_service.requests)
    model_service.replies.append((200, _changed_reply))

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "partial", result.message
    assert len(model_service.requests) == count + 1
    assert "An unrelated new policy" not in json.dumps(model_service.requests[-1])
    assert (scope.wiki_dir / "summaries/normal.md").read_bytes() == normal_summary
    after = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    assert "concepts/shared.md" not in after
    assert after["summaries/normal.md"] == pending["summaries/normal.md"]
    selected = resolve_query_views(kb_dir)
    answer = read_query_page(selected, "concepts/shared.md", view_id=scope.view_id, strict=True)
    assert "The first policy requires review." in answer
    assert "The second policy permits approved use." in answer
    assert "summaries/normal.md" not in selected.views[0].files


def test_independent_summary_can_clear_a_failed_other_source_reason(kb_dir, model_service):
    from openkb.application.documents import import_document
    from openkb.application.query_views import resolve_query_views
    from openkb.application.refresh import refresh_selected_sources, refresh_status
    from openkb.unit_publication import read_head, read_unit_publication

    sources, scope = _prepared(kb_dir, model_service)
    (kb_dir / "second.md").write_text("The second policy has an unfinished replacement.")
    _reply(model_service, {})
    _reply(model_service, {})
    failed = import_document(kb_dir, kb_dir / "second.md")
    assert failed.status == "failed"
    unit = failed.units[0].unit_id
    publication = read_unit_publication(kb_dir, unit, scope.view_id)
    before = read_head(kb_dir, scope.view_id)
    page = "summaries/normal.md"
    body = (scope.wiki_dir / page).read_bytes()
    assert any(r.source_id == sources[1] for r in before.needs_refresh[page])
    count = len(model_service.requests)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope, page_paths=[page]))

    assert result.status == "partial", result.message
    assert len(model_service.requests) == count
    assert (scope.wiki_dir / page).read_bytes() == body
    assert read_unit_publication(kb_dir, unit, scope.view_id) == publication
    after = read_head(kb_dir, scope.view_id)
    assert after.inputs == before.inputs
    assert after.needs_refresh == {p: r for p, r in before.needs_refresh.items() if p != page}
    assert page in resolve_query_views(kb_dir).views[0].files
    assert "summaries/second.md" in refresh_status(kb_dir, scope=scope)["needs_refresh"]


@pytest.mark.parametrize("page", ["summaries/second.md", "concepts/shared.md"])
def test_failed_source_cannot_use_summary_independence_for_its_own_output(
    kb_dir, model_service, page
):
    from openkb.application.documents import import_document
    from openkb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    (kb_dir / "second.md").write_text("The second policy has an unfinished replacement.")
    _reply(model_service, {})
    _reply(model_service, {})
    assert import_document(kb_dir, kb_dir / "second.md").status == "failed"
    before = refresh_status(kb_dir, scope=scope)
    count = len(model_service.requests)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope, page_paths=[page]))

    assert result.status == "failed"
    assert len(model_service.requests) == count
    assert refresh_status(kb_dir, scope=scope) == before


def test_failed_new_source_without_pending_reasons_cannot_refresh_other_summaries(
    kb_dir, model_service
):
    from openkb.application.documents import import_document
    from openkb.application.refresh import refresh_selected_sources, refresh_status
    from openkb.application.views import view_scope

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  retries: 0\n  max_calls: 20\n"
    )
    completed = _import(kb_dir, model_service, "normal", "A complete independent policy.")
    scope = view_scope(kb_dir, completed.units[0].view_id)
    (kb_dir / "unfinished.md").write_text("A new policy that has no successful publication.")
    _reply(model_service, {})
    _reply(model_service, {})
    failed = import_document(kb_dir, kb_dir / "unfinished.md")
    assert failed.status == "failed"
    before = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    index = (scope.wiki_dir / "index.md").read_bytes()
    count = len(model_service.requests)

    result = asyncio.run(
        refresh_selected_sources(
            kb_dir, [failed.source_id], scope=scope, page_paths=["summaries/normal.md"]
        )
    )

    assert result.status == "failed"
    assert len(model_service.requests) == count
    assert (scope.wiki_dir / "index.md").read_bytes() == index
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"] == before


def test_selected_refresh_limits_work_to_requested_pages(kb_dir, model_service):
    from openkb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    before = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    shared = (scope.wiki_dir / "concepts/shared.md").read_bytes()
    count = len(model_service.requests)

    result = asyncio.run(
        refresh_selected_sources(kb_dir, sources, scope=scope, page_paths=["summaries/first.md"])
    )

    assert result.status == "partial", result.message
    assert len(model_service.requests) == count
    assert (scope.wiki_dir / "concepts/shared.md").read_bytes() == shared
    after = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    assert "summaries/first.md" not in after
    assert after == {
        page: reasons for page, reasons in before.items() if page != "summaries/first.md"
    }


@pytest.mark.parametrize(
    "pages",
    [[], ["../outside.md"], ["originals/first.md"], ["summaries/first.md", "summaries/first.md"]],
)
def test_selected_refresh_rejects_invalid_page_scope_before_sending(kb_dir, model_service, pages):
    from openkb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    before = refresh_status(kb_dir, scope=scope)
    count = len(model_service.requests)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope, page_paths=pages))

    assert result.status == "failed"
    assert len(model_service.requests) == count
    assert refresh_status(kb_dir, scope=scope) == before


@pytest.mark.parametrize("late_change", [False, True])
def test_selected_refresh_protects_manual_pages_and_delayed_acceptance(
    kb_dir, model_service, late_change
):
    from openkb.application.pages import read_page, save_page
    from openkb.application.refresh import (
        accept_refresh_proposal,
        read_refresh_proposal,
        refresh_selected_sources,
        refresh_status,
    )

    sources, scope = _prepared(kb_dir, model_service)
    page = read_page(kb_dir, "concepts/shared", scope=scope)
    save_page(
        kb_dir, page.path, page.body + "\nHuman review note.\n", version=page.version, scope=scope
    )
    before = refresh_status(kb_dir, scope=scope)
    model_service.replies.append((200, _changed_reply))

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "awaiting_confirmation", result.message
    assert refresh_status(kb_dir, scope=scope) == before
    candidate = read_refresh_proposal(kb_dir, result.proposal_id, scope=scope)
    assert "The first policy requires review." in candidate["diff"]
    if late_change:
        _import(kb_dir, model_service, "first", "The first policy requires joint review.")
        before = refresh_status(kb_dir, scope=scope)
        assert read_refresh_proposal(kb_dir, result.proposal_id)["status"] == "superseded"
    accepted = accept_refresh_proposal(
        kb_dir, result.proposal_id, version=candidate["version"], scope=scope
    )
    assert accepted.status == ("conflict" if late_change else "completed"), accepted.message
    if late_change:
        assert refresh_status(kb_dir, scope=scope) == before
    else:
        assert not refresh_status(kb_dir, scope=scope)["needs_refresh"]
        assert "Human review note." in (scope.wiki_dir / "concepts/shared.md").read_text()


def test_selected_refresh_requires_full_original_coverage_before_a_model_noop(
    kb_dir, model_service, monkeypatch
):
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    before = refresh_status(kb_dir, scope=scope)
    count = len(model_service.requests)
    monkeypatch.setattr(
        CompileSourceWindows,
        "iter_document_windows",
        lambda self, **kwargs: iter([{"selection_offset": 0, "content": "T"}]),
    )

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "failed"
    assert len(model_service.requests) == count
    assert refresh_status(kb_dir, scope=scope) == before


def test_selected_refresh_cancellation_does_not_publish_prior_staged_work(kb_dir, model_service):
    from openkb.application.execution import ExecutionContext
    from openkb.application.refresh import refresh_selected_sources, refresh_status
    from openkb.locks import LockCancelled

    sources, scope = _prepared(kb_dir, model_service)
    before = refresh_status(kb_dir, scope=scope)
    stopped = False

    def cancel(request):
        nonlocal stopped
        stopped = True
        return _changed_reply(request)

    model_service.replies.append((200, cancel))
    with pytest.raises(LockCancelled):
        asyncio.run(
            refresh_selected_sources(
                kb_dir, sources, scope=scope, context=ExecutionContext(cancelled=lambda: stopped)
            )
        )
    assert refresh_status(kb_dir, scope=scope) == before


def test_selected_refresh_keeps_a_page_with_an_unavailable_source_association(
    kb_dir, model_service
):
    from openkb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    (scope.wiki_dir / "summaries/second.md").unlink()
    before = refresh_status(kb_dir, scope=scope)["needs_refresh"]["concepts/shared.md"]
    count = len(model_service.requests)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "partial", result.message
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"]["concepts/shared.md"] == before
    assert len(model_service.requests) == count


def test_selected_refresh_reads_the_tail_of_a_source_spanning_reader_windows(kb_dir, model_service):
    from openkb.application.refresh import refresh_selected_sources

    sources, scope = _prepared(kb_dir, model_service)
    # More than one reader window, while staying under the short-document token
    # threshold: indexing is deliberately outside this refresh contract test.
    original = "The first policy requires review.\n\n" + (("configuration " * 30 + "\n\n") * 70)
    original += "The final prerequisite is recorded at the end.\n"
    _import(kb_dir, model_service, "first", original)
    count = len(model_service.requests)

    def unchanged(request):
        edit = _editing_request(request)
        return response(
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "status": "unchanged",
                        "base_sha256": edit["base_sha256"],
                        "reason": "The complete source contribution is already covered.",
                    }
                ),
            }
        )

    # This custom endpoint deliberately has no declared capacity. The existing
    # conservative fallback partitions evidence; every SDK batch must finish.
    model_service.replies.extend([(200, unchanged)] * 16)
    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "completed", result.message
    requests = model_service.requests[count:]
    assert 2 <= len(requests) <= 16
    sent = json.dumps([request["messages"] for request in requests])
    assert "The first policy requires review." in sent
    assert "The final prerequisite is recorded at the end." in sent
