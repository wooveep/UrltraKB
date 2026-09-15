"""A bounded repair receives precise diagnostics while retaining original evidence."""

import json

import pytest

from openkb.application.conversations import continue_conversation
from openkb.locks import atomic_write_text
from tests.http_model_fixture import answer_review_response
from tests.test_answer_review_batches import _reader


@pytest.mark.asyncio
async def test_wrong_observation_repair_can_locate_the_unchanged_original(kb_dir, model_service):
    atomic_write_text(kb_dir / "wiki/sources/rows.md", "系统盘自动分区。")
    atomic_write_text(kb_dir / "wiki/sources/after.md", "删除 swap 分区。")
    answer = "删除 swap 分区。"

    def chat(body):
        if any(row["role"] == "tool" for row in body["messages"]):
            return {"role": "assistant", "content": answer}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"read-{i}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": path})},
                }
                for i, path in enumerate(("sources/rows.md", "sources/after.md"))
            ],
        }

    attempts = []

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        attempts.append(payload)
        located = payload.get("protocol_feedback", {}).get("error", {}).get("matching_observations")
        value = answer_review_response(payload)
        value["units"][0]["support"] = [
            {"observation": "o2" if located == ["o2"] else "o1", "quote": "删除 swap 分区"}
        ]
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "系统盘自动分区后还需要做什么？")
    assert result.status == "completed", result
    assert len(attempts) == 2
    assert attempts[0]["observations"] == attempts[1]["observations"]
    assert attempts[0]["answer"] == attempts[1]["answer"] == result.answer


@pytest.mark.asyncio
async def test_citation_repair_identifies_the_rejected_target(kb_dir, model_service):
    target = "sources/snapshots/v-p.md#block-original"
    bad = "sources/snapshots/v-p.md#block-invented"
    answer = f"删除 swap 分区。 [原文]({target})"
    atomic_write_text(kb_dir / "wiki/sources/rows.md", answer)
    reader = _reader(f"删除 swap 分区。 [原文]({bad})")

    def chat(body):
        if bad in body["messages"][-1]["content"]:
            assert not body.get("tools")
            return {"role": "assistant", "content": answer}
        return reader(body)

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    result = await continue_conversation(kb_dir, "自动分区后做什么？")
    assert result.status == "completed", result
    assert result.answer == answer
