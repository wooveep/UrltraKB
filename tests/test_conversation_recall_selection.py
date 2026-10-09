"""Real conversation retrieval must select relevant old turns, not whole topics."""

import json
import re

import pytest
from agents import set_tracing_disabled
from test_context_retrieval import rank_reply
from test_vendor_sdk import response, streamed_response

from openkb.agent.chat_session import ChatSession
from openkb.application.conversations import continue_conversation
from openkb.application.execution import ExecutionContext
from openkb.locks import atomic_write_text

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "facts,expected,first_padding",
    [
        ({1: "My approved personal budget is 17 credits. "}, "17 credits.", None),
        ({2: "My approved personal budget is 17 credits. "}, "17 credits.", None),
        (
            {
                1: "My approved personal budget is 17 credits. ",
                2: "My maximum attendance is 23 people. ",
            },
            "17 credits; 23 people.",
            None,
        ),
        pytest.param(
            {
                1: "My approved personal budget is 17 credits. ",
                2: "My maximum attendance is 23 people. ",
            },
            "17 credits; 23 people.",
            '"' * 12000,
            id="escaped-long-turn",
        ),
    ],
)
async def test_recall_selects_relevant_old_exchange_instead_of_earliest_topic_turn(
    kb_dir, model_service, facts, expected, first_padding
):
    set_tracing_disabled(True)
    atomic_write_text(
        kb_dir / ".openkb/config.yaml",
        "model: openai/gpt-4o-mini\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n",
    )
    atomic_write_text(
        kb_dir / ".env", f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n"
    )
    atomic_write_text(kb_dir / "wiki/sources/manual.md", "A retained original.")
    session = ChatSession.new(kb_dir, "openai/gpt-4o-mini", "en")
    for index in range(9):
        prefix = facts.get(
            index, f"Workspace planning note {index}: retain the exhibition schedule. "
        )
        padding = first_padding if first_padding and index == min(facts) else "workspace " * 3000
        session.record_turn(prefix + padding, "ASSISTANT_POISON: 999 credits.", [])

    def model_reply(request):
        if request.get("stream"):
            current = next(m["content"] for m in request["messages"] if m["role"] == "user")
            if isinstance(current, list):
                current = "".join(part["text"] for part in current if part["type"] == "text")
            intent, _ = json.JSONDecoder().raw_decode(current)
            recalled = [t for t in intent["user_turns"] if t["origin"] == "recalled_user"]
            supported = all(
                any(fact.strip() in t["text"] for t in recalled) for fact in facts.values()
            )
            return streamed_response(
                {"role": "assistant", "content": expected if supported else "Details missing."}
            )
        prompt = request["messages"][-1]["content"]
        if request.get("tools"):
            candidates = re.findall(r"- id: ([^\n]+)\n(.*?)(?=\n- id: |\Z)", prompt, re.S)
            selected = [
                identity
                for identity, text in candidates
                if any(fact.strip() in text for fact in facts.values())
            ]
            selected = selected or [candidates[0][0]]
            reply = rank_reply(selected)
            # A premature model 'done' at a navigation topic must not return that topic.
            reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(
                {"ranked_ids": selected, "done": True}
            )
            return reply
        value = (
            {"title": "Workspace planning"}
            if "Name the subject" in prompt
            else {"belongs_to_current": True}
        )
        return response({"role": "assistant", "content": json.dumps(value)})

    model_service.replies.extend((200, model_reply) for _ in range(12))
    result = await continue_conversation(
        kb_dir,
        "What personal budget and attendance constraints did I set?",
        session_id=session.id,
        context=ExecutionContext(),
    )
    assert result.status == "completed", result
    assert result.answer == expected
    assert result.context["recall"]["turn_ids"] == [session.turns[i].turn_id for i in facts]
    assert result.context["recall"]["context_tokens"] <= 2000
    assert result.context["recall"]["stage_sends"]["retrieval"] <= 8
    assert "ASSISTANT_POISON" not in json.dumps(model_service.requests)
