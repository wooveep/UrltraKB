"""Answer lifecycle reports safe stages and actionable terminal failure codes."""

import json

import pytest

from openkb.application.conversations import continue_conversation
from openkb.application.execution import ExecutionContext
from openkb.locks import atomic_write_text
from tests.http_model_fixture import answer_review_response
from tests.test_answer_review_batches import _reader


@pytest.mark.asyncio
async def test_answer_reports_read_review_and_save_stages(kb_dir, model_service):
    answer = "Partition: 20 GB. [Source](sources/snapshots/v-p.md#block-rows)"
    atomic_write_text(kb_dir / "wiki/sources/rows.md", answer)
    model_service.chat_response = _reader(answer)
    model_service.chat_without_tools = True
    events = []
    result = await continue_conversation(
        kb_dir, "What partition size?", context=ExecutionContext(on_event=events.append)
    )
    assert result.status == "completed", result
    stages = [row["stage"] for row in events if "stage" in row]
    assert "answer_sources" in stages
    assert (
        stages.index("answer_sources")
        < stages.index("answer_review")
        < stages.index("answer_saving")
    )
    counters = [
        step
        for row in events
        if row.get("event") == "progress"
        for step in row["progress"]
        if step["phase"] == "answer_review"
    ]
    assert counters and counters[-1]["completed"] == counters[-1]["total"]


@pytest.mark.asyncio
async def test_invalid_review_retains_a_specific_failure_code(kb_dir, model_service):
    answer = "Partition: 20 GB. [Source](sources/snapshots/v-p.md#block-rows)"
    atomic_write_text(kb_dir / "wiki/sources/rows.md", answer)
    model_service.chat_response = _reader(answer)
    model_service.chat_without_tools = True
    model_service.answer_review_response = lambda body: {"role": "assistant", "content": "{}"}
    result = await continue_conversation(kb_dir, "What partition size?")
    assert result.status == "failed"
    assert "answer_verification_invalid" in result.error


@pytest.mark.asyncio
async def test_independent_review_batches_can_each_recover_once(kb_dir, model_service):
    rows = [f"Partition {i}: {20 + i} GB." for i in range(17)]
    answer = "\n".join(rows)
    atomic_write_text(kb_dir / "wiki/sources/rows.md", answer)
    model_service.chat_response = _reader(answer)
    model_service.chat_without_tools = True
    attempts = {}

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        key = tuple(unit["id"] for unit in payload["units"])
        attempts[key] = attempts.get(key, 0) + 1
        value = answer_review_response(payload)
        if attempts[key] == 1:
            # A reviewer copies a quotation from the wrong observation/wording.
            value["units"][0]["support"][0]["quote"] = "Not in the original."
        else:
            assert payload["protocol_feedback"]["error"]["problem"] == "quote_mismatch"
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "List the partition sizes.")
    assert result.status == "completed", result
    assert len(attempts) == 3 and set(attempts.values()) == {2}
