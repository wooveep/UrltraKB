"""A normal answer completes within its retrieval/generation request allowance."""

import json

import pytest

from openkb.agent.chat_session import load_session
from openkb.agent.query import build_run_config_from_bundle, run_query
from openkb.application.conversations import ask_question, continue_conversation
from openkb.application.execution import ExecutionContext
from openkb.config import load_config, save_config
from openkb.locks import atomic_write_text, kb_ingest_lock


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["conversation", "question", "query", "terminal"])
async def test_answer_completes_after_retrieval_without_review(kb_dir, model_service, entry):
    answer = "单盘：选择 Custom 后自动分区，删除 swap，释放空间加到 /home。"
    atomic_write_text(kb_dir / "wiki/index.md", answer)

    def chat(body):
        if any(row["role"] == "tool" for row in body["messages"]):
            return {"role": "assistant", "content": answer}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "source",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "index.md"}),
                    },
                }
            ],
        }

    model_service.chat_response = chat
    model_service.answer_review_response = lambda body: {"role": "assistant", "content": "{}"}
    config = load_config(kb_dir / ".openkb/config.yaml")
    config["processing"]["max_requests"] = 2
    save_config(kb_dir / ".openkb/config.yaml", config)
    events = []
    context = ExecutionContext(on_event=events.append)
    if entry in {"conversation", "question"}:
        operation = continue_conversation if entry == "conversation" else ask_question
        result = await operation(kb_dir, "系统盘的分区操作？", context=context)
        assert result.status == "completed", result
        assert result.answer == answer
        if entry == "conversation":
            saved = load_session(kb_dir, result.session_id)
            assert saved.turn_count == 1 and saved.assistant_texts == [answer]
    else:
        with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir) as bundle:
            actual = await run_query(
                "系统盘的分区操作？",
                kb_dir,
                config["model"],
                stream=entry == "terminal",
                raw=True,
                bundle=bundle,
                run_config=build_run_config_from_bundle(config["model"], bundle),
            )
        assert actual == answer
    assert len(model_service) == 2
    assert "answer_review" not in [event.get("stage") for event in events]
