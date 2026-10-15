"""Retrieve short committed user facts with real indexing, ranking and delivery."""

import json

import pytest
from agents import Runner, set_tracing_disabled

from ultrakb.agent.chat_session import ChatSession
from ultrakb.agent.query import build_query_agent, build_run_config_from_bundle
from ultrakb.context.conversation import ConversationMemory
from ultrakb.context.preparation import IntentTurn, PreparedConversationInput
from ultrakb.context.recall import recall_users, selected_user_excerpts
from ultrakb.locks import atomic_write_text, kb_ingest_lock, session_lock
from ultrakb.model_tokenizer import TokenMeasure

pytest_plugins = ("test_vendor_sdk",)


def measure():
    return TokenMeasure(
        {"unknown_reason": None, "input_limit": 65536, "measurement_model": "gpt-4o-mini"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "facts,expected,first_padding",
    [
        ({1: "My approved personal budget is 17 credits. "}, ("17", "credits"), None),
        ({2: "My approved personal budget is 17 credits. "}, ("17", "credits"), None),
        (
            {
                1: "My approved personal budget is 17 credits. ",
                2: "My maximum attendance is 23 people. ",
            },
            ("17", "23"),
            None,
        ),
        pytest.param(
            {
                1: "My approved personal budget is 17 credits. ",
                2: "My maximum attendance is 23 people. ",
            },
            ("17", "23"),
            '"' * 12000,
            id="escaped-long-turn",
        ),
    ],
)
async def test_recall_selects_relevant_old_exchange_instead_of_earliest_topic_turn(
    kb_dir, model_service, facts, expected, first_padding
):
    from test_llm_execution import executor_for

    set_tracing_disabled(True)
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    for index in range(3):
        prefix = facts.get(index, "My exhibition dates are April 10 to April 12. ")
        padding = first_padding if first_padding and index == min(facts) else ""
        session.record_turn(prefix + padding, "ASSISTANT_POISON: 999 credits.", [])
    token_measure = measure()
    refs = [
        {
            "turn_id": session.turns[i].turn_id,
            "order": session.turns[i].order,
            "content_digest": session.turns[i].content_digest,
        }
        for i in facts
    ]
    if first_padding:
        # Escaping and shared locator budgets are application operations, not model ability tests.
        items = selected_user_excerpts(session.turns, refs, set(), token_measure, budget=500)
        assert [item["turn_id"] for item in items] == [session.turns[i].turn_id for i in facts]
        assert token_measure.payload_tokens(items) <= 500
        assert all(item["text"].startswith(facts[i]) for item, i in zip(items, facts))
        assert items[0]["char_end"] < len(session.turns[min(facts)].user_text)
        assert items[1]["text"] == session.turns[max(facts)].user_text
        assert model_service.requests == []
        return

    executor = executor_for(model_service, max_calls=30, retries=0)
    executor.question_capacity = token_measure
    question = "What personal budget and attendance constraints did I set?"
    from ultrakb.application.query_views import resolve_query_views

    selection = resolve_query_views(kb_dir, question)
    with session_lock(kb_dir, session.id), kb_ingest_lock(kb_dir / ".ultrakb"):
        memory = ConversationMemory(session, executor)
        state = memory.catch_up()
        assert state.indexed_turns == 3 and state.pending_turns == 0
        items, recall = recall_users(
            memory, state, question, selection, set(), budget=1000, select_k=len(facts)
        )
    assert recall["status"] == "completed", recall
    actual_ids = {item["turn_id"] for item in items}
    assert {session.turns[i].turn_id for i in facts} == actual_ids
    assert session.turns[0].turn_id not in actual_ids
    assert recall["context_tokens"] <= 1000
    assert recall["stage_sends"]["retrieval"] <= 8
    assert "ASSISTANT_POISON" not in json.dumps(model_service.requests)

    prepared = PreparedConversationInput(
        question, recalled=tuple(IntentTurn(**item) for item in items)
    )
    atomic_write_text(
        kb_dir / "wiki/index.md", "# Exhibition\nUser constraints are in supplied intent."
    )
    with executor.activate():
        agent = build_query_agent(
            str(kb_dir / "wiki"), "deepseek/deepseek-flash", bundle=model_service.credentials
        )
        agent.instructions += (
            "\nFor this personal-preference question, use the supplied conversation_intent data. "
            "Return the supported budget and attendance values in one sentence."
        )
        result = await Runner.run(
            agent,
            prepared.sdk_input,
            max_turns=3,
            run_config=build_run_config_from_bundle(
                "deepseek/deepseek-flash", model_service.credentials
            ),
        )
    answer = str(result.final_output).lower()
    assert all(value in answer for value in expected), answer
    assert "999" not in answer
    final = model_service.requests[-1]
    assert "recalled_user" in json.dumps(final["messages"])
    assert "ASSISTANT_POISON" not in json.dumps(final["messages"])
