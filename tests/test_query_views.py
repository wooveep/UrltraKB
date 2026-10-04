"""Version selection fixes the actual readable evidence before a model sees it."""

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
    kb_dir, monkeypatch, name, version, fact, family="installation", *, product="WinStack"
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
        ),
    )
    assert result.status == "added"
    return result


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


def test_product_name_ambiguity_is_visible_and_cannot_fall_back(kb_dir, monkeypatch):
    from openkb.agent.query_evidence import evidence_answer, selection_catalog
    from openkb.application.query_views import resolve_query_views

    products = ("CNware WinStack", "CNware-WinStack", "CNware WinStack 虚拟化云平台")
    imported = [
        _import_rule(kb_dir, monkeypatch, f"manual-{i}", "9.4.0", "Source rule.", product=product)
        for i, product in enumerate(products)
    ]
    (kb_dir / "wiki/concepts/connection.md").write_text("Legacy rule.")
    selection = resolve_query_views(kb_dir, "CNware WinStack 的安装、运维和最佳实践？")
    assert selection.views == ()
    assert selection.missing
    for rendered in (selection_catalog(selection), evidence_answer("", selection)):
        assert "ambiguous" in rendered.lower()
        for product, source in zip(products, imported):
            assert product in rendered
            assert source.units[0].view_id in rendered
        assert "9.4.0" in rendered
        assert "--view" in rendered

    explicit = resolve_query_views(kb_dir, "CNware WinStack 虚拟化云平台 V9.4.0 的安装？")
    assert [view.view_id for view in explicit.views] == [imported[-1].units[0].view_id]


def test_confirmed_product_identity_includes_all_three_original_sources(kb_dir, monkeypatch):
    from openkb.application.query_views import read_query_page, resolve_query_views
    from openkb.application.version_review import (
        resume_version_review,
        review_source_version,
        supplement_version_reviews,
    )
    from openkb.view_records import SourceMetadata

    records = [
        _import_rule(kb_dir, monkeypatch, name, "9.4.0", fact, family=family, product=product)
        for name, family, product, fact in (
            ("installation", "installation", "CNware-WinStack", "Server and disk conditions."),
            ("maintenance", "maintenance", "CNware WinStack Platform", "K3s restore conditions."),
            ("practice", "practice", "CNware WinStack", "Practice rule."),
        )
    ]
    assert resolve_query_views(kb_dir, "CNware WinStack V9.4.0").views == ()
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "description": "Confirmed source",
                                "content": "Compiled knowledge.",
                                "create": [],
                                "update": [],
                                "related": [],
                            }
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    for record in records[:2]:
        review = review_source_version(kb_dir, record.source_id)
        supplement_version_reviews(
            kb_dir, {review.review_id: SourceMetadata(product="CNware WinStack")}
        )
        assert resume_version_review(kb_dir, review.review_id).status == "added"
    selection = resolve_query_views(kb_dir, "CNware WinStack V9.4.0")
    assert len(selection.views) == 1 and not selection.missing
    assert set(selection.views[0].source_revision_ids) == {
        record.source_revision_id for record in records
    }
    for name, fact in (
        ("installation", "Server and disk conditions."),
        ("maintenance", "K3s restore conditions."),
        ("practice", "Practice rule."),
    ):
        assert fact in read_query_page(
            selection, f"sources/{name}.md", view_id=selection.views[0].view_id
        )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("WinStackX V2 configuration?", {"WinStackX"}),
        ("WinStack Pro V2 configuration?", {"WinStack Pro"}),
        ("Compare WinStackX and WinStack Pro V2", {"WinStackX", "WinStack Pro"}),
        ("WinStack V2 configuration?", set()),
    ],
)
def test_product_candidates_respect_word_boundaries_and_identity(
    kb_dir, monkeypatch, question, expected
):
    from openkb.application.query_views import resolve_query_views

    for i, product in enumerate(("WinStack", "WinStackX", "WinStack Pro")):
        _import_rule(kb_dir, monkeypatch, f"manual-{i}", "2", "Source rule.", product=product)
    selection = resolve_query_views(kb_dir, question)
    assert {view.product for view in selection.views} == expected
    assert bool(selection.missing) == (not expected)


def test_explicit_product_scope_still_checks_versions_and_history(kb_dir, monkeypatch):
    from openkb.application.query_views import read_query_page, resolve_query_views
    from openkb.application.views import view_scope

    first = _import_rule(
        kb_dir, monkeypatch, "chosen", "9.4.0", "Original recovery.", product="CNware-WinStack"
    )
    live = view_scope(kb_dir, first.units[0].view_id)
    pinned = resolve_query_views(kb_dir, "CNware WinStack V9.4.0", scope=live).views[0]
    history = view_scope(kb_dir, live.view_id, historical_revision=pinned.knowledge_revision_id)
    _import_rule(
        kb_dir, monkeypatch, "later", "9.4.0", "New instruction.", product="CNware-WinStack"
    )
    _import_rule(
        kb_dir, monkeypatch, "other", "9.3.1", "Other instruction.", product="CNware WinStack"
    )
    gap = resolve_query_views(kb_dir, "CNware WinStack V9.3.1", scope=live)
    assert gap.views == ()
    assert "9.3.1" in " ".join(gap.missing)
    selected = resolve_query_views(kb_dir, "CNware WinStack V9.4.0", scope=history)
    assert selected.views[0].knowledge_revision_id == pinned.knowledge_revision_id
    assert "Original recovery." in read_query_page(
        selected, "sources/chosen.md", view_id=live.view_id
    )
    assert "later" not in " ".join(selected.views[0].files)


@pytest.fixture
def ambiguous_products(kb_dir, monkeypatch):
    for i, product in enumerate(("CNware-WinStack", "CNware WinStack")):
        _import_rule(kb_dir, monkeypatch, f"manual-{i}", "9.4.0", "Source rule.", product=product)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint", ["question", "conversation", "query", "query-stream", "skill"]
)
async def test_ambiguous_product_query_reports_candidates_without_model_calls(
    kb_dir, monkeypatch, ambiguous_products, entrypoint
):
    def unexpected(**kwargs):
        pytest.fail("An ambiguous evidence scope must not call the model")

    monkeypatch.setattr("litellm.completion", unexpected)
    monkeypatch.setattr("litellm.acompletion", unexpected)
    question = "CNware WinStack V9.4.0 recovery?"
    if entrypoint in {"query", "query-stream"}:
        from openkb.agent.query import run_query

        answer = await run_query(question, kb_dir, "test", stream=entrypoint == "query-stream")
    elif entrypoint == "skill":
        from test_skill_runner import _install_skill

        from openkb.agent.skill_runner import run_skill

        _install_skill(kb_dir, "summarize")
        with pytest.raises(ValueError, match="Ambiguous") as failure:
            await run_skill(skill_name="summarize", intent=question, kb_dir=kb_dir, model="test")
        answer = str(failure.value)
    else:
        from openkb.application.conversations import ask_question, continue_conversation

        operation = ask_question if entrypoint == "question" else continue_conversation
        result = await operation(kb_dir, question)
        assert result.status == "completed", result.error
        answer = result.answer
    assert "ambiguous" in answer.lower()
    assert "CNware-WinStack" in answer and "CNware WinStack" in answer


@pytest.fixture
def opposing_versions(kb_dir, monkeypatch):
    return [
        _import_rule(kb_dir, monkeypatch, f"manual-{version}", version, fact)
        for version, fact in (("1", "TLS is required."), ("2", "TLS is forbidden."))
    ]


@pytest.mark.parametrize("label", ["V2", "R3"])
def test_explicit_version_accepts_the_saved_applicability_label(kb_dir, monkeypatch, label):
    from openkb.application.query_views import resolve_query_views

    _import_rule(kb_dir, monkeypatch, "base", "9", "TLS is required.")
    _import_rule(kb_dir, monkeypatch, "custom", label, "TLS is forbidden.")
    selection = resolve_query_views(kb_dir, f"WinStack version {label}")
    assert [view.applicable_versions for view in selection.views] == [(label,)]
    assert selection.missing == ()


def test_different_series_defaults_keep_only_each_series_input(
    kb_dir, opposing_versions, monkeypatch
):
    from openkb.application.query_views import (
        read_query_page,
        resolve_query_views,
        select_default_view,
    )
    from openkb.source_catalog import read_source

    first, second = opposing_versions
    network = _import_rule(
        kb_dir, monkeypatch, "network", "2", "Port 443 is open.", family="network"
    )
    for source in (first, network):
        select_default_view(
            kb_dir, read_source(kb_dir, source.source_id).family_id, source.units[0].view_id
        )
    selection = resolve_query_views(kb_dir, "How is the network configured?")
    assert len(selection.views) == 2
    assert "Port 443 is open." in read_query_page(
        selection, "sources/network.md", view_id=network.units[0].view_id
    )
    assert "TLS is forbidden." not in read_query_page(
        selection, "summaries/manual-2.md", view_id=second.units[0].view_id
    )


def test_no_default_keeps_opposing_versions_in_separate_read_scopes(kb_dir, opposing_versions):
    from openkb.application.query_views import read_query_page, resolve_query_views

    selection = resolve_query_views(kb_dir, "How is TLS configured?")
    assert {view.applicable_versions for view in selection.views} == {("1",), ("2",)}
    first, second = opposing_versions
    old = read_query_page(selection, "summaries/manual-1.md", view_id=first.units[0].view_id)
    new = read_query_page(selection, "summaries/manual-2.md", view_id=second.units[0].view_id)
    assert "TLS is required." in old and "TLS is forbidden." not in old
    assert "TLS is forbidden." in new and "TLS is required." not in new
    assert first.source_revision_id in old and second.source_revision_id in new


@pytest.mark.asyncio
async def test_query_model_cannot_read_another_version(kb_dir, opposing_versions, monkeypatch):
    from litellm import ModelResponse

    from openkb.agent.query import run_query

    first, second = opposing_versions
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def model(**kwargs):
        outputs = [item["content"] for item in kwargs["messages"] if item["role"] == "tool"]
        message = (
            {"content": outputs[-1]}
            if outputs
            else {
                "tool_calls": [
                    {
                        "id": "read-wrong-version",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps(
                                {
                                    "path": "summaries/manual-1.md",
                                    "view_id": first.units[0].view_id,
                                }
                            ),
                        },
                    }
                ]
            }
        )
        return ModelResponse(
            choices=[
                {
                    "message": {"role": "assistant", **message},
                    "finish_reason": "stop" if outputs else "tool_calls",
                }
            ]
        )

    monkeypatch.setattr("litellm.acompletion", model)
    answer = await run_query("How does WinStack V2 configure TLS?", kb_dir, "openai/gpt-4o-mini")
    assert "TLS is required." not in answer
    assert second.source_revision_id in answer
    assert "本次允许的证据范围" in answer


def test_source_reader_reports_the_version_of_its_published_body(kb_dir, opposing_versions):
    from openkb.documents import read_document_source

    first, _ = opposing_versions
    source = read_document_source(kb_dir, first.source_id)
    assert source["version_metadata"]["product"] == "WinStack"
    assert source["version_metadata"]["applicable_versions"] == ["1"]
    assert source["source_revision_id"] == first.source_revision_id


def test_unpublished_version_is_a_gap_and_does_not_hide_legacy(kb_dir):
    from openkb.application.documents import import_document
    from openkb.application.query_views import resolve_query_views
    from openkb.view_records import SourceMetadata

    (kb_dir / "wiki/concepts/legacy.md").write_text("Legacy connection notes.")
    path = kb_dir / "broken.pdf"
    path.write_bytes(b"not a PDF")
    result = import_document(
        kb_dir,
        path,
        metadata=SourceMetadata(
            product="WinStack",
            applicable_versions=("2",),
            family="installation",
        ),
    )
    assert result.status == "failed"
    assert [view.view_id for view in resolve_query_views(kb_dir).views] == ["legacy"]
    assert [view.view_id for view in resolve_query_views(kb_dir, "WinStack TLS?").views] == [
        "legacy"
    ]
    explicit = resolve_query_views(kb_dir, "WinStack V2 TLS?")
    assert explicit.views == ()
    assert explicit.missing


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["question", "conversation", "http-query", "http-chat"])
async def test_streamed_entrypoints_use_the_series_default(
    kb_dir, opposing_versions, monkeypatch, entrypoint
):
    from openkb.application.query_views import select_default_view
    from openkb.source_catalog import read_source

    first, second = opposing_versions
    select_default_view(
        kb_dir, read_source(kb_dir, first.source_id).family_id, first.units[0].view_id
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)

    async def model(**kwargs):
        outputs = [item["content"] for item in kwargs["messages"] if item["role"] == "tool"]
        delta = (
            {"content": outputs[-1]}
            if outputs
            else {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "other-version",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps(
                                {
                                    "path": "summaries/manual-2.md",
                                    "view_id": second.units[0].view_id,
                                }
                            ),
                        },
                    }
                ]
            }
        )

        return _chat_chunks(delta, "stop" if outputs else "tool_calls")

    monkeypatch.setattr("litellm.acompletion", model)
    if entrypoint == "question":
        from openkb.application.conversations import ask_question

        result = await ask_question(kb_dir, "How is TLS configured?")
        assert result.status == "completed", result.error
        answer = result.answer
    elif entrypoint == "conversation":
        from openkb.application.conversations import continue_conversation

        result = await continue_conversation(kb_dir, "How is TLS configured?")
        assert result.status == "completed", result.error
        answer = result.answer
    else:
        from fastapi.testclient import TestClient

        from openkb.api import create_app

        monkeypatch.setenv("OPENKB_KB_ROOT", str(kb_dir.parent))
        endpoint = entrypoint.removeprefix("http-")
        response = TestClient(create_app()).post(
            f"/api/v1/{endpoint}",
            json={
                "kb": kb_dir.name,
                "question" if endpoint == "query" else "message": "How is TLS configured?",
                "stream": True,
            },
        )
        assert response.status_code == 200
        answer = response.text
    assert "TLS is forbidden." not in answer
    assert first.source_revision_id in answer


@pytest.mark.asyncio
async def test_chat_cannot_reuse_previous_versions_tool_evidence(
    kb_dir, opposing_versions, monkeypatch
):
    from openkb.application.conversations import continue_conversation

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def model(**kwargs):
        messages = kwargs["messages"]
        question = [item["content"] for item in messages if item["role"] == "user"][-1]
        version = "2" if "V2" in question else "1"
        outputs = [item["content"] for item in messages if item["role"] == "tool"]
        if outputs:
            delta = {"content": outputs[-1]}
        else:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"read-{version}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps(
                                {
                                    "path": f"summaries/manual-{version}.md",
                                    "view_id": opposing_versions[int(version) - 1].units[0].view_id,
                                }
                            ),
                        },
                    }
                ]
            }
        return _chat_chunks(delta, "stop" if outputs else "tool_calls")

    monkeypatch.setattr("litellm.acompletion", model)
    first = await continue_conversation(kb_dir, "WinStack V1 TLS?")
    assert "TLS is required." in first.answer
    second = await continue_conversation(kb_dir, "WinStack V2 TLS?", session_id=first.session_id)
    assert "TLS is forbidden." in second.answer
    assert "TLS is required." not in second.answer


def test_explicit_family_default_survives_import_order(kb_dir, opposing_versions, monkeypatch):
    from openkb.application.documents import import_document
    from openkb.application.query_views import (
        read_query_page,
        resolve_query_views,
        select_default_view,
    )
    from openkb.source_catalog import read_source
    from openkb.view_records import SourceMetadata

    first, second = opposing_versions
    family = read_source(kb_dir, first.source_id).family_id
    select_default_view(kb_dir, family, first.units[0].view_id)
    path = kb_dir / "manual-3.pdf"
    path.write_bytes((kb_dir / "manual-2.pdf").read_bytes())
    replies = iter([{"description": "Another version", "content": "Use TLS only on port 443."}, {}])
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(replies))))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    assert (
        import_document(
            kb_dir,
            path,
            metadata=SourceMetadata(
                product="WinStack", applicable_versions=("3",), family="installation"
            ),
        ).status
        == "added"
    )
    selection = resolve_query_views(kb_dir, "How is TLS configured?")
    assert [view.applicable_versions for view in selection.views] == [("1",)]
    with pytest.raises(ValueError, match="scope"):
        read_query_page(selection, "summaries/manual-2.md", view_id=second.units[0].view_id)


@pytest.mark.parametrize(
    "question, expected",
    [
        ("How does WinStack V2 use TLS?", {("2",)}),
        ("Use WinStack V1.", {("1",)}),
        ("WinStack V1怎么配置TLS？", {("1",)}),
        ("Which versions support TLS?", {("1",)}),
        ("What version should I use?", {("1",)}),
        ("Compare WinStack V1 and V2 TLS rules", {("1",), ("2",)}),
        ("How does WinStack V99 use TLS?", set()),
    ],
)
def test_questions_respect_defaults_and_explicit_versions(
    kb_dir, opposing_versions, question, expected
):
    from openkb.application.query_views import resolve_query_views, select_default_view
    from openkb.source_catalog import read_source

    first, _ = opposing_versions
    select_default_view(
        kb_dir, read_source(kb_dir, first.source_id).family_id, first.units[0].view_id
    )
    selection = resolve_query_views(kb_dir, question)
    assert {view.applicable_versions for view in selection.views} == expected
    if not expected:
        assert selection.missing


def test_legacy_remains_readable_but_cannot_supply_a_requested_version(kb_dir):
    from openkb.application.query_views import read_query_page, resolve_query_views

    (kb_dir / "wiki/concepts/connection.md").write_text("Historical instruction: disable TLS.")
    legacy = resolve_query_views(kb_dir, "How was TLS configured?")
    assert "disable TLS" in read_query_page(legacy, "concepts/connection.md", view_id="legacy")
    target = resolve_query_views(kb_dir, "How is TLS configured in V2?")
    assert target.views == () and target.missing
    history = resolve_query_views(kb_dir, "V2 TLS rules, with historical reference")
    assert history.views[0].reference_only
    assert "Historical reference only" in read_query_page(
        history, "concepts/connection.md", view_id="legacy"
    )


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
