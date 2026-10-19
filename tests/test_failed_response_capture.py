"""Retain malformed delivery inputs verbatim, independently of accepted checkpoints."""

import hashlib
from types import SimpleNamespace

import pytest
from compile_work_fixtures import accepted_payload, checkpoint_record, invalid_responses

from ultrakb.compile_checkpoints import digest

pytest_plugins = ("compile_work_fixtures",)


@pytest.mark.asyncio
async def test_failed_initial_and_repair_responses_are_retained(kb_dir, monkeypatch, compile_work):
    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.agent.page_response import PageResponseError

    checkpoint = compile_work({"document": "manual"}, {"protocol": "summary-json"})
    checkpoint.save("summary:1", digest("first"), {"description": "Accepted", "sections": []})
    retained = accepted_payload(checkpoint, "summary:1")
    # These are explicit malformed protocol inputs, not simulated valid model knowledge.
    initial = ' \n{"description":"initial","unexpected":[]}\n '
    repaired = ' \n{"description":"repair","sections":"wrong-type"}\n '
    sends = []

    async def invalid_repair(*args, on_response=None, **kwargs):
        sends.append(args)
        on_response(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content=repaired), finish_reason="stop")
                ]
            )
        )
        return repaired.strip()

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call_async", invalid_repair)
    with pytest.raises(PageResponseError, match="summary_invalid_shape"):
        await _llm_call_page_async(
            "unused-offline",
            [{"role": "user", "content": "Return the selected summary sections"}],
            "document-summary:3",
            initial_response=initial,
            response_protocol="summary-json",
            expected_sections=("section-1",),
            checkpoints=checkpoint,
            checkpoint_key="summary:3",
        )
    assert len(sends) == 1
    saved_responses = invalid_responses(checkpoint, "summary:3")
    records = [
        next(record for record in saved_responses if record["raw"] == value.encode())
        for value in (initial, repaired)
    ]
    assert [record["attempt"] for record in records] == [1, 2]
    for record, expected in zip(records, (initial, repaired), strict=True):
        assert record["raw"] == expected.encode("utf-8")
        assert record["response_digest"] == hashlib.sha256(record["raw"]).hexdigest()
        assert record["stage"] == "document-summary:3"
        assert record["response_protocol"] == "summary-json"
        assert record["expected_sections"] == ["section-1"]
        assert record["code"] == "summary_invalid_shape"
        assert "Expected description and sections" in record["detail"]
        assert any(field["path"] == "$.sections" for field in record["field_errors"])
        assert record["request_digest"]
    assert accepted_payload(checkpoint, "summary:1") == retained
    assert checkpoint_record(checkpoint, "summary:3")["failure_code"] == "summary_invalid_shape"
    assert checkpoint.load("summary:3", records[-1]["request_digest"]) is None


@pytest.mark.asyncio
async def test_manual_retry_failure_keeps_diagnostic_without_another_send(
    kb_dir, monkeypatch, compile_work
):
    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import digest

    checkpoint = compile_work({"document": "manual"}, {"protocol": "summary-json"})
    messages = [{"role": "user", "content": "Selected summary sections"}]
    checkpoint.claim_repair("summary:3", digest(messages))

    async def forbidden(*args, **kwargs):
        pytest.fail("An exhausted repair must not generate another model response")

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call_async", forbidden)
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        await _llm_call_page_async(
            "unused-offline",
            messages,
            "document-summary:3",
            initial_response='{"content":"wrong protocol"}',
            response_protocol="summary-json",
            expected_sections=("section-1",),
            checkpoints=checkpoint,
            checkpoint_key="summary:3",
        )
    [record] = invalid_responses(checkpoint, "summary:3")
    assert record["raw"] == b'{"content":"wrong protocol"}'
    assert record["code"] == "summary_invalid_shape"


def test_publication_cleanup_preserves_native_responses_and_accepted_work(
    kb_dir, monkeypatch, compile_work
):
    from ultrakb.desktop.verification_data import publish_pages

    checkpoint = compile_work({"document": "manual"}, {"protocol": "summary-json"})
    path = checkpoint.record_invalid_response(
        "summary:3",
        digest("request"),
        '{"description":"missing sections"}',
        stage="document-summary:3",
        response_protocol="summary-json",
        expected_sections=("section-1",),
        code="summary_invalid_shape",
        detail="Expected description and sections",
        attempt=1,
    )
    checkpoint.save("summary:3", digest("request"), {"description": "Accepted", "sections": []})
    publish_pages(kb_dir, {"concepts/later.md": b"A later committed publication."})
    [record] = invalid_responses(checkpoint, "summary:3")
    assert record["reference"] == path
    assert record["raw"] == b'{"description":"missing sections"}'
    assert record["code"] == "summary_invalid_shape"
    assert accepted_payload(checkpoint, "summary:3") == {"description": "Accepted", "sections": []}


@pytest.mark.asyncio
async def test_truncated_repair_keeps_the_received_body(kb_dir, monkeypatch, compile_work):
    from ultrakb.agent.compiler import TruncatedResponseError, _llm_call_page_async
    from ultrakb.agent.page_response import PageResponseError

    checkpoint = compile_work({"document": "manual"}, {"protocol": "summary-json"})
    partial = ' \n{"description":"incomplete","sections":['

    async def interrupted_repair(*args, on_response, **kwargs):
        on_response(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=partial), finish_reason="length"
                    )
                ]
            )
        )
        raise TruncatedResponseError("Incomplete summary receipt")

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call_async", interrupted_repair)
    with pytest.raises(PageResponseError, match="page_truncated_response"):
        await _llm_call_page_async(
            "unused-offline",
            [{"role": "user", "content": "Selected summary"}],
            "document-summary:3",
            initial_response="{}",
            response_protocol="summary-json",
            checkpoints=checkpoint,
            checkpoint_key="summary:3",
        )
    records = invalid_responses(checkpoint, "summary:3")
    repaired = next(record for record in records if record["attempt"] == 2)
    assert repaired["code"] == "page_truncated_response"
    assert repaired["raw"] == partial.encode("utf-8")
