"""Retain malformed delivery inputs verbatim, independently of accepted checkpoints."""

import hashlib
import json
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_failed_initial_and_repair_responses_are_retained(kb_dir, monkeypatch):
    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import CompileCheckpoints

    checkpoint = CompileCheckpoints(
        kb_dir,
        SimpleNamespace(task_id="a" * 32, parent_task_id=None),
        {"document": "manual"},
        {"protocol": "summary-json"},
    )
    checkpoint.save("summary:1", "first", {"description": "Accepted", "sections": []})
    retained = {path: path.read_bytes() for path in checkpoint.directory.glob("payload-*.json")}
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
    with pytest.raises(PageResponseError, match="summary_invalid_shape") as failure:
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
    records = sorted(
        (json.loads(path.read_text()) for path in checkpoint.failure_directory.glob("*.json")),
        key=lambda record: record["attempt"],
    )
    assert len(records) == 2, "The original invalid response and its repair must both survive"
    for record, expected in zip(records, (initial, repaired), strict=True):
        raw = kb_dir / record["response_file"]
        assert raw.read_bytes() == expected.encode("utf-8")
        assert record["response_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
        assert record["stage"] == "document-summary:3"
        assert record["checkpoint_key"] == "summary:3"
        assert record["response_protocol"] == "summary-json"
        assert record["expected_sections"] == ["section-1"]
        assert record["error_code"] == "summary_invalid_shape"
        assert "Expected description and sections" in record["error_detail"]
        assert record["field_errors"]
        assert any(field["path"] == "$.sections" for field in record["field_errors"])
        assert record["request_digest"]
        assert str(raw.relative_to(kb_dir)) in str(failure.value) or record["attempt"] == 1
    assert all(path.read_bytes() == before for path, before in retained.items())
    manifest = json.loads(checkpoint.path.read_text())
    assert manifest["entries"]["summary:3"]["failure_code"] == "summary_invalid_shape"
    assert len(manifest["entries"]["summary:3"]["response_failures"]) == 2
    assert checkpoint.load("summary:3", records[-1]["request_digest"]) is None


@pytest.mark.asyncio
async def test_manual_retry_failure_keeps_diagnostic_without_another_send(kb_dir, monkeypatch):
    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import CompileCheckpoints, digest

    checkpoint = CompileCheckpoints(
        kb_dir,
        SimpleNamespace(task_id="b" * 32, parent_task_id=None),
        {"document": "manual"},
        {"protocol": "summary-json"},
    )
    messages = [{"role": "user", "content": "Selected summary sections"}]
    checkpoint.claim_repair("summary:3", digest(messages))

    async def forbidden(*args, **kwargs):
        pytest.fail("An exhausted repair must not generate another model response")

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call_async", forbidden)
    with pytest.raises(PageResponseError, match="compile_repair_exhausted") as failure:
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
    [raw] = checkpoint.failure_directory.glob("*.txt")
    assert raw.read_text() == '{"content":"wrong protocol"}'
    assert str(raw.relative_to(kb_dir)) in str(failure.value)


def test_successful_publication_cleanup_preserves_failed_response_evidence(kb_dir, monkeypatch):
    from ultrakb.compile_checkpoints import CompileCheckpoints, cleanup_published_checkpoints

    checkpoint = CompileCheckpoints(
        kb_dir,
        SimpleNamespace(task_id="c" * 32, parent_task_id=None),
        {"document": "manual"},
        {"protocol": "summary-json"},
    )
    path = checkpoint.record_invalid_response(
        "summary:3",
        "request",
        '{"description":"missing sections"}',
        stage="document-summary:3",
        response_protocol="summary-json",
        expected_sections=("section-1",),
        code="summary_invalid_shape",
        detail="Expected description and sections",
        attempt=1,
    )
    checkpoint.save("summary:3", "request", {"description": "Accepted", "sections": []})
    monkeypatch.setattr(
        "ultrakb.compile_checkpoints.document_checkpoints", lambda *args: checkpoint
    )
    cleanup_published_checkpoints(kb_dir, "legacy", "manual")
    assert not checkpoint.directory.exists()
    raw = kb_dir / path
    assert raw.read_text() == '{"description":"missing sections"}'
    assert json.loads(raw.with_suffix(".json").read_text())["error_code"] == "summary_invalid_shape"


@pytest.mark.asyncio
async def test_truncated_repair_keeps_the_received_body(kb_dir, monkeypatch):
    from ultrakb.agent.compiler import TruncatedResponseError, _llm_call_page_async
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import CompileCheckpoints

    checkpoint = CompileCheckpoints(
        kb_dir,
        SimpleNamespace(task_id="d" * 32, parent_task_id=None),
        {"document": "manual"},
        {"protocol": "summary-json"},
    )
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
    records = [json.loads(path.read_text()) for path in checkpoint.failure_directory.glob("*.json")]
    repaired = next(record for record in records if record["attempt"] == 2)
    assert repaired["error_code"] == "page_truncated_response"
    assert (kb_dir / repaired["response_file"]).read_bytes() == partial.encode("utf-8")
