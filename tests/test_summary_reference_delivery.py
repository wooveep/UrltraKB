"""Structured-reference prompts, strict SDK serialization and retained identity."""

import json
from types import SimpleNamespace

import pytest


def test_fresh_checkpoint_selects_structured_delivery_but_old_requests_are_retained(kb_dir):
    from ultrakb.compile_checkpoints import CompileCheckpoints

    checkpoint = CompileCheckpoints(
        kb_dir, SimpleNamespace(task_id="d" * 32, parent_task_id=None), {"document": "guide"}, {}
    )
    assert checkpoint.structured_summary
    checkpoint.prepare_summary_delivery()
    assert checkpoint.structured_summary
    assert json.loads(checkpoint.path.read_text())["entries"]["protocol:summary"]["version"] == 2
    legacy = CompileCheckpoints(
        kb_dir, SimpleNamespace(task_id="e" * 32, parent_task_id=None), {"document": "guide"}, {}
    )
    legacy.save("summary:1", "original-request", {"description": "Kept", "sections": []})
    before = legacy.path.read_bytes()
    assert not legacy.structured_summary
    assert legacy.prepare_summary_delivery()
    assert legacy.path.read_bytes() == before


def test_structured_summary_prompt_and_schema_use_the_same_supplied_ids():
    from ultrakb.agent.document_summary import summary_messages, summary_request_protocol
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.agent.summary_protocol import summary_response_format

    window = {"content": "Use this value.\n\nsetting=KEEP\n", "supplementary_context": []}
    messages = summary_messages(
        {"role": "system", "content": "Explain originals."}, "guide", window, structured=True
    )
    assert summary_request_protocol(messages) == "summary-parts-json"
    [catalog] = [
        json.loads(m["content"].split("\n", 1)[1])
        for m in messages
        if m["content"].startswith("Structured original")
    ]
    ids = tuple(SourceExcerpts([window]).references)
    assert tuple(r["id"] for r in catalog["references"]) == ids
    response_format = summary_response_format(
        "summary-parts-json", ("section-1",), model="deepseek/deepseek-flash", excerpt_ids=ids
    )
    schema = response_format["json_schema"]["schema"]
    part = schema["properties"]["sections"]["items"]["properties"]["parts"]["items"]
    assert part["properties"]["refs"]["items"]["enum"] == list(ids)
    assert part["required"] == ["kind", "text", "refs"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reference_schema_uses_real_sdk_responses_wire_without_an_online_model(monkeypatch):
    import litellm
    from error_transport import ErrorTransport

    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            LlmCredentialBundle(api_key="unused-offline", base_url="https://api.deepseek.com"),
        ),
        ModelCallPolicy(max_calls=1, retries=0),
    )
    real = litellm.acompletion
    excerpts = SourceExcerpts(["setting=KEEP\n"])
    with ErrorTransport(status=400) as endpoint:

        async def local(**kwargs):
            return await real(**{**kwargs, "base_url": endpoint.url})

        monkeypatch.setattr(litellm, "acompletion", local)
        with executor.activate(), pytest.raises(litellm.BadRequestError):
            await _llm_call_page_async(
                "deepseek/deepseek-flash",
                [{"role": "user", "content": "Local strict JSON schema request."}],
                "document-summary:1",
                response_protocol="summary-parts-json",
                expected_sections=("s1",),
                original_excerpts=excerpts,
            )
        [request] = endpoint.requests
        assert request["path"].endswith("/responses")
        fmt = request["json"]["text"]["format"]
        assert fmt["type"] == "json_schema" and fmt["strict"] is True
        part = fmt["schema"]["properties"]["sections"]["items"]["properties"]["parts"]["items"]
        assert part["properties"]["refs"]["items"]["enum"] == list(excerpts.references)
        assert executor.calls == 1


def test_resolution_receipt_preserves_existing_repair_flags(kb_dir):
    from ultrakb.compile_checkpoints import CompileCheckpoints

    checkpoint = CompileCheckpoints(
        kb_dir, SimpleNamespace(task_id="f" * 32, parent_task_id=None), {"document": "guide"}, {}
    )
    checkpoint.claim_repair("summary:3", "retained")
    before = checkpoint.path.read_text()
    path = checkpoint.record_delivery_resolution(
        "summary:3", "retained", "raw", "assembled", [{"mode": "whitespace"}]
    )
    assert (kb_dir / path).is_file()
    old = json.loads(before)["entries"]["summary:3"]
    new = json.loads(checkpoint.path.read_text())["entries"]["summary:3"]
    assert all(new[key] == value for key, value in old.items())


def test_inherited_v1_protocol_does_not_upgrade_the_child_request(kb_dir):
    from ultrakb.compile_checkpoints import CompileCheckpoints
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.locks import atomic_write_json
    from ultrakb.model_task import TaskJournal

    bindings = RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle())
    first = TaskJournal(kb_dir, CompletionExecutor(bindings, ModelCallPolicy(max_calls=10)))
    parent = CompileCheckpoints(kb_dir, first.record, {"document": "guide"}, {})
    parent.save("summary:1", "retained-request", {"description": "Kept", "sections": []})
    record = json.loads(parent.path.read_text())
    record["entries"]["protocol:summary"] = {"version": 1}
    atomic_write_json(parent.path, record)
    before = parent.path.read_bytes()
    expanded = TaskJournal(
        kb_dir,
        CompletionExecutor(bindings, ModelCallPolicy(max_calls=20)),
        resume=first.record.task_id,
    )
    child = CompileCheckpoints(kb_dir, expanded.record, parent.identity, parent.conditions)
    assert not child.structured_summary
    child.prepare_summary_delivery()
    assert not child.structured_summary
    assert child.load("summary:1", "retained-request")["description"] == "Kept"
    assert parent.path.read_bytes() == before


@pytest.mark.asyncio
async def test_accepted_structured_reference_keeps_its_identity_after_restart(kb_dir):
    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.agent.document_summary import build_document_summary
    from ultrakb.agent.page_response import parse_summary
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.compile_checkpoints import CompileCheckpoints
    from ultrakb.locks import atomic_write_text

    atomic_write_text(
        kb_dir / "wiki/sources/guide.md",
        "X86 only.\n\nsetting=KEEP\n\nARM only.\n\nsetting=KEEP\n\n",
    )
    checkpoint = CompileCheckpoints(
        kb_dir, SimpleNamespace(task_id="c" * 32, parent_task_id=None), {"document": "guide"}, {}
    )
    checkpoint.prepare_summary_delivery()
    windows = CompileSourceWindows(kb_dir / "wiki", "guide", navigation=[])
    selected = []

    async def from_saved_protocol(messages, stage, originals, sections):
        # Exercise the real delivery boundary with local protocol data, no transport.
        excerpts = SourceExcerpts(originals)
        reference_id = next(
            key
            for key, match in excerpts.references.items()
            if key.startswith("r")
            and match.start == 36
            and excerpts.index.body(match) == "setting=KEEP"
        )
        selected.append(reference_id)
        raw = json.dumps(
            {
                "description": "Two processor conditions",
                "sections": [
                    {
                        "id": identity,
                        "parts": [
                            {"kind": "prose", "text": "ARM only.", "refs": []},
                            {"kind": "excerpt", "text": "", "refs": [reference_id]},
                        ],
                    }
                    for identity in sections
                ],
            }
        )
        delivered = await _llm_call_page_async(
            "deepseek/deepseek-flash",
            messages,
            stage,
            initial_response=raw,
            response_protocol="summary-parts-json",
            expected_sections=sections,
            original_excerpts=excerpts,
            checkpoints=checkpoint,
            checkpoint_key="summary:1",
        )
        return parse_summary(delivered, sections)

    first = await build_document_summary(
        windows,
        system_message={"role": "system", "content": "Explain originals."},
        generate=from_saved_protocol,
        fits=lambda messages: True,
        checkpoints=checkpoint,
    )

    async def forbidden(*args):
        raise AssertionError("An accepted reference must not trigger generation on restart")

    restored = await build_document_summary(
        windows,
        system_message={"role": "system", "content": "Explain originals."},
        generate=forbidden,
        fits=lambda messages: True,
        checkpoints=checkpoint,
    )
    assert first.markdown == restored.markdown
    assert "setting=KEEP" in restored.markdown
    assert "ARM only." in restored.markdown
    payload = json.loads(next(checkpoint.directory.glob("payload-*.json")).read_text())
    assert payload["result"]["sections"][0]["parts"][1]["refs"] == selected
    manifest = json.loads(checkpoint.path.read_text())
    [receipt] = manifest["entries"]["summary:1"]["delivery_resolutions"]
    stored = json.loads((kb_dir / receipt).read_text())
    assert stored["resolutions"][0]["source"]["window_span"][0] == 36


@pytest.mark.asyncio
async def test_unknown_reference_keeps_its_position_and_exhausted_repair_gate(kb_dir, monkeypatch):
    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.compile_checkpoints import CompileCheckpoints, digest

    checkpoint = CompileCheckpoints(
        kb_dir, SimpleNamespace(task_id="b" * 32, parent_task_id=None), {"document": "guide"}, {}
    )
    excerpts = SourceExcerpts(["setting=1\n"], request_format=checkpoint.request_version)
    messages = excerpts.messages(
        [{"role": "user", "content": "The retained third batch"}], structured=True
    )
    checkpoint.claim_repair("summary:3", digest(messages))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Offline diagnosis must not call a model")

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call_async", forbidden)
    raw = json.dumps(
        {
            "description": "Guide",
            "sections": [
                {"id": "s1", "parts": [{"kind": "excerpt", "text": "", "refs": ["unknown"]}]}
            ],
        }
    )
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        await _llm_call_page_async(
            "unused-offline",
            messages,
            "document-summary:3",
            response_protocol="summary-parts-json",
            expected_sections=("s1",),
            original_excerpts=excerpts,
            initial_response=raw,
            checkpoints=checkpoint,
            checkpoint_key="summary:3",
        )
    [sidecar] = checkpoint.failure_directory.glob("*.json")
    record = json.loads(sidecar.read_text())
    [issue] = record["field_errors"]
    assert issue["path"] == "$.sections[0].parts[0].refs[0]"
    assert issue["rule"] == "excerpt_unknown_reference"
    assert (kb_dir / record["response_file"]).read_text() == raw


@pytest.mark.asyncio
async def test_structured_summary_requires_original_windows_before_any_send(monkeypatch):
    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.agent.page_delivery import validate_page_delivery

    async def forbidden(*args, **kwargs):
        raise AssertionError("A missing source context must be rejected before transport")

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call_async", forbidden)
    with pytest.raises(ValueError, match="original source"):
        await _llm_call_page_async(
            "unused-offline",
            [{"role": "user", "content": "A structured summary without its sources"}],
            "document-summary:1",
            response_protocol="summary-parts-json",
            expected_sections=("s1",),
        )
    with pytest.raises(ValueError, match="original source"):
        validate_page_delivery(
            "{}", response_protocol="summary-parts-json", expected_sections=("s1",)
        )
