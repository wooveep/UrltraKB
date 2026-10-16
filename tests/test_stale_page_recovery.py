"""Exercise the real page reception/repair driver without fabricated model replies."""

import asyncio
import json
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace

import litellm
import pytest
from error_transport import ErrorTransport

from ultrakb.agent.page_response import PageResponseError
from ultrakb.agent.page_updates import PageUpdate
from ultrakb.agent.source_excerpts import SourceExcerpts
from ultrakb.compile_checkpoints import CompileCheckpoints, digest
from ultrakb.config import LlmCredentialBundle
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
from ultrakb.llm_usage import UsageScope, read_requests
from ultrakb.locks import atomic_write_text


def page_inputs(kb_dir):
    annotation = "Human annotation: verify the maintenance window."
    original = annotation + "\n\nOld recovery rule.\n"
    source = "During recovery inspect /system/config/state.\n"
    path = kb_dir / "wiki/concepts/recovery.md"
    atomic_write_text(path, original)
    patch = PageUpdate(original, preserved_fragments=(annotation,))
    excerpts = SourceExcerpts([source])
    messages = [
        {"role": "system", "content": "Compile only the supplied original facts. Return JSON."},
        {"role": "user", "content": "Original:\n" + source},
        patch.message(),
        {
            "role": "user",
            "content": "Replace only the old recovery rule with the original recovery instruction. "
            "Use an inline code span for the exact original path. Retain the human annotation.",
        },
    ]
    raw = json.dumps(
        {
            "base_sha256": patch.digest,
            "edits": [
                {
                    "block_id": patch.request()["blocks"][-1]["id"],
                    "operation": "replace",
                    "content": "[[original:x9.9]]\n",
                }
            ],
        }
    )
    checkpoints = CompileCheckpoints(
        kb_dir,
        SimpleNamespace(task_id="b" * 32, parent_task_id=None, compiler_request_version=2),
        {"document": "manual"},
        {"protocol": "stale-page-originals"},
    )
    return path, patch, excerpts, messages, raw, checkpoints


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("repair_used", [False, True], ids=["repair-available", "repair-used"])
@pytest.mark.parametrize("retained", [False, True], ids=["fresh-delivery", "retained-delivery"])
@pytest.mark.parametrize("proposal_only", [False, True], ids=["assembled-page", "proposal"])
async def test_stale_delivery_never_sends_a_repair_or_changes_its_checkpoint(
    kb_dir, record_property, repair_used, retained, proposal_only
):
    from ultrakb.agent.compiler import _llm_call_page_async

    path, patch, excerpts, messages, raw, checkpoints = page_inputs(kb_dir)
    signature = digest(excerpts.messages(messages))
    key = "page:concepts/recovery.md"
    if repair_used:
        checkpoints.claim_repair(key, signature)
    if retained:
        checkpoints.save(key, signature, raw)
        raw = checkpoints.load(key, signature)
    before_checkpoint = checkpoints.path.read_bytes() if checkpoints.path.exists() else None
    human = patch.original + "\nHuman addition made during generation.\r\n"
    atomic_write_text(path, human)
    before_page = path.read_bytes()

    with ErrorTransport(status=401) as service:
        with pytest.raises(PageResponseError) as caught:
            await _llm_call_page_async(
                "deepseek/deepseek-flash",
                messages,
                key,
                bundle=LlmCredentialBundle(
                    api_key="offline-test-key", base_url=service.url, timeout=1
                ),
                initial_response=raw,
                page_update=patch,
                read_current=lambda: path.read_bytes().decode("utf-8"),
                original_excerpts=excerpts,
                checkpoints=checkpoints,
                checkpoint_key=key,
                proposal_only=proposal_only,
            )
        assert caught.value.code == "page_update_stale"
        assert service.requests == []
        record_property("http_sends", len(service.requests))
    assert path.read_bytes() == before_page
    assert not read_requests(kb_dir)
    after_checkpoint = checkpoints.path.read_bytes() if checkpoints.path.exists() else None
    assert after_checkpoint == before_checkpoint


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, litellm.AuthenticationError), (503, litellm.ServiceUnavailableError)],
)
async def test_failed_repair_keeps_human_content_and_cannot_reopen_its_send_allowance(
    kb_dir, status, error_type, request
):
    from ultrakb.agent.compiler import _llm_call_page_async

    path, patch, excerpts, messages, raw, checkpoints = page_inputs(kb_dir)
    key = "page:concepts/recovery.md"
    before = path.read_bytes()
    with ErrorTransport(status=status) as service:
        credentials = LlmCredentialBundle(
            api_key="offline-test-key", base_url=service.url, timeout=1
        )
        executor = CompletionExecutor(
            RoleBindings("deepseek/deepseek-flash", credentials),
            ModelCallPolicy(max_calls=1, retries=0, backoff=0, deadline=monotonic() + 5),
            scope=UsageScope(kb_dir, "c" * 32),
        )
        options = {
            "bundle": credentials,
            "initial_response": raw,
            "page_update": patch,
            "read_current": lambda: path.read_bytes().decode("utf-8"),
            "original_excerpts": excerpts,
            "checkpoints": checkpoints,
            "checkpoint_key": key,
            "response_format": {"type": "json_object"},
            "max_tokens": 512,
            "reasoning_effort": "none",
        }
        with executor.activate(), pytest.raises(error_type) as caught:
            await _llm_call_page_async("deepseek/deepseek-flash", messages, key, **options)
        assert caught.value.status_code == status
        assert service.body["error"]["message"] in str(caught.value)
        with executor.activate(), pytest.raises(PageResponseError) as exhausted:
            await _llm_call_page_async("deepseek/deepseek-flash", messages, key, **options)
        assert exhausted.value.code == "compile_repair_exhausted"
        assert len(service.requests) == executor.calls == 1
        request.node.user_properties.append(("http_sends", len(service.requests)))
    assert path.read_bytes() == before
    entry = json.loads(checkpoints.path.read_text())["entries"][key]
    assert entry["repair_used"] and entry["failure_code"] == "page_excerpt_invalid"
    assert checkpoints.load(key, digest(excerpts.messages(messages))) is None
    [failed] = read_requests(kb_dir)
    assert failed.state == "failed" and failed.send_status == "sent"
    assert failed.usage_status == "unknown"
    assert failed.input_total is None and failed.output_total is None


@pytest.mark.live
@pytest.mark.asyncio
async def test_current_page_repairs_only_the_unknown_reference_with_real_flash(
    kb_dir, monkeypatch, live_llm
):
    from ultrakb.agent.compiler import _llm_call_page_async

    path, patch, excerpts, messages, raw, checkpoints = page_inputs(kb_dir)
    signature = digest(excerpts.messages(messages))
    key = "page:concepts/recovery.md"
    live_llm.install(monkeypatch)
    before = len(live_llm.calls)
    credentials = replace(live_llm.credentials, timeout=30)
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", credentials),
        ModelCallPolicy(max_calls=1, retries=0, backoff=0, deadline=monotonic() + 45),
    )
    options = {
        "bundle": credentials,
        "initial_response": raw,
        "page_update": patch,
        "read_current": lambda: path.read_bytes().decode("utf-8"),
        "original_excerpts": excerpts,
        "checkpoints": checkpoints,
        "checkpoint_key": key,
        "response_format": {"type": "json_object"},
        "max_tokens": 512,
        "reasoning_effort": "none",
    }
    with executor.activate():
        result = await asyncio.wait_for(
            _llm_call_page_async("deepseek/deepseek-flash", messages, key, **options), timeout=45
        )
    delivered = json.loads(result)
    assert len(live_llm.calls) - before == 1
    sent = live_llm.calls[-1]
    assert sent["model"] == "deepseek/deepseek-flash"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["max_tokens"] == 512
    assert sent["extra_body"]["reasoning_effort"] == "none"
    wire = live_llm.requests[-1]
    assert wire["model"] == "deepseek-flash" and wire["max_tokens"] == 512
    assert wire["reasoning_effort"] == "none" and wire["thinking"] == {"type": "disabled"}
    assert "page_excerpt_invalid" in str(sent["messages"])
    assert "`/system/config/state`" in delivered["content"]
    assert "/systen/config/state" not in delivered["content"]
    assert patch.preserved_fragments[0] in delivered["content"]
    assert path.read_bytes() == patch.original.encode()
    with executor.activate(), pytest.raises(PageResponseError) as caught:
        await _llm_call_page_async("deepseek/deepseek-flash", messages, key, **options)
    assert caught.value.code == "compile_repair_exhausted"
    assert len(live_llm.calls) - before == 1
    assert executor.calls == 1
    assert path.read_bytes() == patch.original.encode()
    entry = json.loads(checkpoints.path.read_text())["entries"][key]
    assert entry["request_digest"] == signature and entry["repair_used"]
    assert entry["failure_code"] == "page_excerpt_invalid"
