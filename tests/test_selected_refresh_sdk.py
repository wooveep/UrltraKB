"""Selected refresh uses real SDK requests and atomic publication on disposable KBs."""

import asyncio
import json

import pytest
from native_catalog_fixtures import document_name, published_files, unit_inventory

pytest_plugins = ("test_vendor_sdk",)


def _import(kb, service, name, text, *, shared=False):
    from native_catalog_fixtures import list_sources
    from publication_fixtures import publish_document

    path = kb / f"{name}.md"
    path.write_text(text)
    return publish_document(
        kb,
        path,
        text,
        bundle=service.credentials,
        shared_body="# Shared\n\nThe first policy permits direct use.\n\n" + text
        if shared
        else None,
        shared_sources=[f"summaries/{document_name(kb, 'first')}.md"]
        if shared and any(source.label == "first.md" for source in list_sources(kb))
        else None,
    )


def _prepared(kb, service):
    from ultrakb.application.views import view_scope

    (kb / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nlanguage: en\n"
        "model_capacity:\n  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
        "model_policy:\n  retries: 0\n  max_calls: 20\n"
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
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    normal = scope.wiki_dir / f"summaries/{document_name(kb_dir, 'normal')}.md"
    before = normal.read_bytes()
    count = len(model_service.requests)

    def forbidden(*args, **kwargs):
        raise AssertionError("Selected refresh must not run a document compiler")

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", forbidden)
    monkeypatch.setattr("ultrakb.agent.compiler.compile_long_doc", forbidden)
    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "completed", result.message
    requests = model_service.requests[count:]
    assert 1 <= len(requests) <= 4
    text = json.dumps([request["messages"] for request in requests])
    assert "The first policy requires review." in text
    assert "The second policy permits approved use." in text
    assert "An independent policy remains available." not in text
    assert normal.read_bytes() == before
    assert not refresh_status(kb_dir, scope=scope)["needs_refresh"]


def test_selected_refresh_failure_retains_live_pages_and_pending_reasons(
    kb_dir, model_service, monkeypatch
):
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    before = refresh_status(kb_dir, scope=scope)
    pages = published_files(kb_dir)
    import httpx

    def disconnected(client, request):
        raise httpx.ConnectError("Local connection unavailable", request=request)

    monkeypatch.setattr(httpx.Client, "_send_single_request", disconnected)
    monkeypatch.setattr(httpx.AsyncClient, "_send_single_request", disconnected)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "failed"
    assert refresh_status(kb_dir, scope=scope) == before
    assert published_files(kb_dir) == pages


def test_selected_refresh_never_clears_an_unselected_source_reason(kb_dir, model_service):
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status

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


def test_selected_refresh_publishes_both_source_changes_together(kb_dir, model_service):
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)

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
    from publication_fixtures import fail_document

    from ultrakb.application.query_views import read_query_page, resolve_query_views
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    normal_summary = (
        scope.wiki_dir / f"summaries/{document_name(kb_dir, 'normal')}.md"
    ).read_bytes()
    (kb_dir / "normal.md").write_text("An unrelated new policy requires review.")
    failed = fail_document(kb_dir, kb_dir / "normal.md", bundle=model_service.credentials)
    assert failed.status == "failed"
    pending = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    count = len(model_service.requests)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "partial", result.message
    assert count < len(model_service.requests) <= count + 4
    assert "An unrelated new policy" not in json.dumps(model_service.requests[-1])
    assert (
        scope.wiki_dir / f"summaries/{document_name(kb_dir, 'normal')}.md"
    ).read_bytes() == normal_summary
    after = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    assert "concepts/shared.md" not in after
    assert (
        after[f"summaries/{document_name(kb_dir, 'normal')}.md"]
        == pending[f"summaries/{document_name(kb_dir, 'normal')}.md"]
    )
    selected = resolve_query_views(kb_dir)
    answer = read_query_page(selected, "concepts/shared.md", view_id=scope.view_id, strict=True)
    assert "The first policy requires review." in answer
    assert "The second policy permits approved use." in answer
    assert f"summaries/{document_name(kb_dir, 'normal')}.md" not in selected.views[0].files


def test_independent_summary_can_clear_a_failed_other_source_reason(kb_dir, model_service):
    from publication_fixtures import fail_document

    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status
    from ultrakb.application.views import published_scope

    sources, scope = _prepared(kb_dir, model_service)
    (kb_dir / "second.md").write_text("The second policy has an unfinished replacement.")
    failed = fail_document(kb_dir, kb_dir / "second.md", bundle=model_service.credentials)
    assert failed.status == "failed"
    unit = failed.units[0].unit_id
    publication = unit_inventory(kb_dir, unit, scope.view_id)
    before = published_scope(kb_dir, scope.view_id).knowledge
    before_pending = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    page = f"summaries/{document_name(kb_dir, 'normal')}.md"
    body = (scope.wiki_dir / page).read_bytes()
    assert any(r["source_id"] == sources[1] for r in before_pending[page])
    count = len(model_service.requests)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope, page_paths=[page]))

    assert result.status == "partial", result.message
    assert len(model_service.requests) == count
    assert (scope.wiki_dir / page).read_bytes() == body
    assert unit_inventory(kb_dir, unit, scope.view_id) == publication
    after = published_scope(kb_dir, scope.view_id).knowledge
    assert after.unit_revisions() == before.unit_revisions()
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"] == {
        p: r for p, r in before_pending.items() if p != page
    }
    assert page in resolve_query_views(kb_dir).views[0].files
    assert (
        f"summaries/{document_name(kb_dir, 'second')}.md"
        in refresh_status(kb_dir, scope=scope)["needs_refresh"]
    )


@pytest.mark.parametrize("page", ["summaries/second.md", "concepts/shared.md"])
def test_failed_source_cannot_use_summary_independence_for_its_own_output(
    kb_dir, model_service, page
):
    from publication_fixtures import fail_document

    from ultrakb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    if page == "summaries/second.md":
        page = f"summaries/{document_name(kb_dir, 'second')}.md"
    (kb_dir / "second.md").write_text("The second policy has an unfinished replacement.")
    assert (
        fail_document(kb_dir, kb_dir / "second.md", bundle=model_service.credentials).status
        == "failed"
    )
    before = refresh_status(kb_dir, scope=scope)
    count = len(model_service.requests)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope, page_paths=[page]))

    assert result.status == "failed"
    assert len(model_service.requests) == count
    assert refresh_status(kb_dir, scope=scope) == before


def test_failed_new_source_without_pending_reasons_cannot_refresh_other_summaries(
    kb_dir, model_service
):
    from publication_fixtures import fail_document

    from ultrakb.application.refresh import refresh_selected_sources, refresh_status
    from ultrakb.application.views import view_scope

    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nlanguage: en\n"
        "model_policy:\n  retries: 0\n  max_calls: 20\n"
    )
    completed = _import(kb_dir, model_service, "normal", "A complete independent policy.")
    scope = view_scope(kb_dir, completed.units[0].view_id)
    (kb_dir / "unfinished.md").write_text("A new policy that has no successful publication.")
    failed = fail_document(kb_dir, kb_dir / "unfinished.md", bundle=model_service.credentials)
    assert failed.status == "failed"
    before = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    index = (scope.wiki_dir / "index.md").read_bytes()
    count = len(model_service.requests)

    result = asyncio.run(
        refresh_selected_sources(
            kb_dir,
            [failed.source_id],
            scope=scope,
            page_paths=[f"summaries/{document_name(kb_dir, 'normal')}.md"],
        )
    )

    assert result.status == "failed"
    assert len(model_service.requests) == count
    assert (scope.wiki_dir / "index.md").read_bytes() == index
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"] == before


def test_selected_refresh_limits_work_to_requested_pages(kb_dir, model_service):
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    before = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    shared = (scope.wiki_dir / "concepts/shared.md").read_bytes()
    count = len(model_service.requests)

    result = asyncio.run(
        refresh_selected_sources(
            kb_dir,
            sources,
            scope=scope,
            page_paths=[f"summaries/{document_name(kb_dir, 'first')}.md"],
        )
    )

    assert result.status == "partial", result.message
    assert len(model_service.requests) == count
    assert (scope.wiki_dir / "concepts/shared.md").read_bytes() == shared
    after = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    assert f"summaries/{document_name(kb_dir, 'first')}.md" not in after
    assert after == {
        page: reasons
        for page, reasons in before.items()
        if page != f"summaries/{document_name(kb_dir, 'first')}.md"
    }


@pytest.mark.parametrize(
    "pages",
    [[], ["../outside.md"], ["originals/first.md"], ["summaries/first.md", "summaries/first.md"]],
)
def test_selected_refresh_rejects_invalid_page_scope_before_sending(kb_dir, model_service, pages):
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    pages = [
        f"summaries/{document_name(kb_dir, 'first')}.md" if path == "summaries/first.md" else path
        for path in pages
    ]
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
    from ultrakb.application.pages import read_page, save_page
    from ultrakb.application.refresh import (
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
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status

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


def test_selected_refresh_cancellation_does_not_publish_prior_staged_work(
    kb_dir, model_service, monkeypatch
):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status
    from ultrakb.locks import LockCancelled

    sources, scope = _prepared(kb_dir, model_service)
    before = refresh_status(kb_dir, scope=scope)
    stopped = False

    import litellm

    original = litellm.acompletion

    async def cancel(**kwargs):
        nonlocal stopped
        result = await original(**kwargs)
        stopped = True
        return result

    monkeypatch.setattr(litellm, "acompletion", cancel)
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
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status

    sources, scope = _prepared(kb_dir, model_service)
    (scope.wiki_dir / f"summaries/{document_name(kb_dir, 'second')}.md").unlink()
    before = refresh_status(kb_dir, scope=scope)["needs_refresh"]["concepts/shared.md"]
    count = len(model_service.requests)

    result = asyncio.run(refresh_selected_sources(kb_dir, sources, scope=scope))

    assert result.status == "partial", result.message
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"]["concepts/shared.md"] == before
    assert len(model_service.requests) == count


def test_selected_refresh_reads_the_tail_of_a_source_spanning_reader_windows(kb_dir, model_service):
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.selected_refresh import _full_windows

    _, scope = _prepared(kb_dir, model_service)
    original = "The first policy requires review.\n\n"
    original += "Configuration records must be retained.\n\n" * 8
    original += "The final prerequisite is recorded at the end.\n"
    _import(kb_dir, model_service, "first", original)
    from ultrakb.application.views import published_scope
    from ultrakb.storage.compile_inputs import materialize_inputs

    base = published_scope(kb_dir).knowledge
    with base.compile_workspace() as baseline:
        materialize_inputs(base, baseline, None)
        reader = CompileSourceWindows(baseline.directory, document_name(kb_dir, "first"))
        complete = _full_windows(reader)
        bounded = list(
            reader.iter_document_windows(fits=lambda window: len(window["content"]) <= 180)
        )
    assert 2 <= len(bounded) <= 16
    assert "".join(window["content"] for window in bounded) == original
    assert "The final prerequisite is recorded at the end." in bounded[-1]["content"]
    assert "".join(window["content"] for window in complete) == original
    assert model_service.requests == []
