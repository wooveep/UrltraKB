"""A later located discrepancy gets one further bounded, fully reviewed correction."""

import json

import pytest

from openkb.agent.chat_session import load_session
from openkb.application.conversations import continue_conversation
from openkb.locks import atomic_write_text
from tests.http_model_fixture import answer_review_response
from tests.test_answer_review_batches import _reader


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["correct", "out_of_scope", "still_wrong"])
async def test_later_discrepancy_is_repaired_with_current_unit_scope(
    kb_dir, model_service, behavior
):
    core = "删除 swap 分区，空间加到 /home。"
    first = "单盘场景先选择 Custom，再点击自动分区。"
    second = "Custom 与自动分区是同一流程中的操作。"
    bad_first = "所有场景都自动分区。"
    bad_second = "Custom 与自动分区互斥。"
    source = "\n".join((first, second, core))
    atomic_write_text(kb_dir / "wiki/sources/rows.md", source)
    reader = _reader("\n".join((bad_first, bad_second, core)))
    repairs, reviews = [], []

    def chat(body):
        raw = body["messages"][-1]["content"]
        if '"stage": "answer_correction"' not in raw:
            return reader(body)
        payload = json.loads(raw)
        repairs.append(payload)
        if len(repairs) == 1:
            assert payload["editable_units"] == ["u1"]
            edit = {"unit": "u1", "text": first}
        else:
            assert len(repairs) == 2
            assert payload["answer"] == "\n".join((first, bad_second, core))
            assert payload["editable_units"] == ["u2"]
            edit = {
                "unit": "u3" if behavior == "out_of_scope" else "u2",
                "text": "Custom 排除自动分区。" if behavior == "still_wrong" else second,
            }
        return {"role": "assistant", "content": json.dumps({"edits": [edit]})}

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        assert len(reviews) <= 3
        assert core in payload["answer"]
        assert payload["observations"] == reviews[0]["observations"]
        value = answer_review_response(payload)
        # The first pass locates one flaw; full re-review then locates a second.
        index = 0 if bad_first in payload["answer"] else 1
        claim = payload["units"][index]["text"]
        if claim != (first if index == 0 else second):
            value["units"][index].update(verdict="unsupported", support=[])
            value.update(
                verdict="unsupported",
                issues=[
                    {
                        "kind": "unsupported",
                        "units": [payload["units"][index]["id"]],
                        "claim": claim,
                        "reason": first if index == 0 else second,
                    }
                ],
            )
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "系统盘自动分区和删除 swap 有什么要求？")
    assert len(repairs) == 2, result
    saved = load_session(kb_dir, result.session_id)
    if behavior == "correct":
        assert result.status == "completed", result
        assert result.answer == source
        assert saved.assistant_texts == [source] and saved.turn_count == 1
        assert not any(row.get("role") == "developer" for row in saved.history)
    else:
        assert result.status == "failed", result
        assert not saved.assistant_texts and saved.turn_count == 0
        category = (
            "answer_correction_invalid"
            if behavior == "out_of_scope"
            else "answer_evidence_unsupported"
        )
        assert category in result.error
