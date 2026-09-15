"""One citation recovery receives the exact rejected target."""

import pytest

from openkb.application.conversations import continue_conversation
from openkb.locks import atomic_write_text
from tests.http_model_fixture import wiki_source_answer as _reader


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
