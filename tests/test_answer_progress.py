"""Public answer stages finish after retrieval and generation without semantic review."""

import pytest

from openkb.application.conversations import continue_conversation
from openkb.application.execution import ExecutionContext
from openkb.locks import atomic_write_text
from tests.http_model_fixture import wiki_source_answer


@pytest.mark.asyncio
async def test_answer_reports_read_draft_and_save_stages(kb_dir, model_service):
    answer = "Partition: 20 GB. [Source](sources/snapshots/v-p.md#block-rows)"
    atomic_write_text(kb_dir / "wiki/sources/rows.md", answer)
    model_service.chat_response = wiki_source_answer(answer)
    events = []
    result = await continue_conversation(
        kb_dir, "What partition size?", context=ExecutionContext(on_event=events.append)
    )
    assert result.status == "completed", result
    stages = [row["stage"] for row in events if "stage" in row]
    assert stages.index("answer_sources") < stages.index("answer_drafting")
    assert stages.index("answer_drafting") < stages.index("answer_saving")
    assert "answer_review" not in stages
    assert len(model_service) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [1, 17])
async def test_answer_does_not_depend_on_a_review_service(kb_dir, model_service, rows):
    answer = "\n".join(f"Partition {i}: {20 + i} GB." for i in range(rows))
    atomic_write_text(kb_dir / "wiki/sources/rows.md", answer)
    model_service.chat_response = wiki_source_answer(answer)
    reviews = []

    def invalid_review(body):
        reviews.append(body)
        return {"role": "assistant", "content": "{}"}

    model_service.answer_review_response = invalid_review
    result = await continue_conversation(kb_dir, "List the partition sizes.")
    assert result.status == "completed" and result.answer == answer
    assert result.turn_count == 1
    assert not reviews and len(model_service) == 2
