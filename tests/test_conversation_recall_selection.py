"""Retrieve short committed user facts with real indexing, ranking and delivery."""

import json

import pytest
from agents import Runner, set_tracing_disabled
from native_usage_fixtures import observe_executor

from ultrakb.agent.query import build_query_agent, build_run_config_from_bundle
from ultrakb.application.conversation_memory import ConversationMemory
from ultrakb.application.stored_conversation_context import user_turns
from ultrakb.application.stored_conversation_recall import recall_stored_users
from ultrakb.application.views import published_scope
from ultrakb.context.preparation import IntentTurn, PreparedConversationInput
from ultrakb.context.recall import selected_user_excerpts
from ultrakb.desktop.verification_data import publish_pages
from ultrakb.model_tokenizer import TokenMeasure
from ultrakb.storage.session_store import SessionStore

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
    store = SessionStore(kb_dir, published_scope(kb_dir).knowledge.identity)
    store.create("recall", model="deepseek/deepseek-flash", language="en")
    for index in range(3):
        prefix = facts.get(index, "My exhibition dates are April 10 to April 12. ")
        padding = first_padding if first_padding and index == min(facts) else ""
        store.append_exchange(
            "recall", f"turn-{index}", prefix + padding, "ASSISTANT_POISON: 999 credits."
        )
    conversation = store.load("recall")
    turns = user_turns(conversation)
    token_measure = measure()
    refs = [
        {
            "turn_id": turns[i].turn_id,
            "order": turns[i].order,
            "content_digest": turns[i].content_digest,
        }
        for i in facts
    ]
    if first_padding:
        # Escaping and shared locator budgets are application operations, not model ability tests.
        items = selected_user_excerpts(turns, refs, set(), token_measure, budget=500)
        assert [item["turn_id"] for item in items] == [turns[i].turn_id for i in facts]
        assert token_measure.payload_tokens(items) <= 500
        assert all(item["text"].startswith(facts[i]) for item, i in zip(items, facts))
        assert items[0]["char_end"] < len(turns[min(facts)].user_text)
        assert items[1]["text"] == turns[max(facts)].user_text
        assert model_service.requests == []
        return

    executor = observe_executor(kb_dir, executor_for(model_service, max_calls=30, retries=0))
    executor.question_capacity = token_measure
    question = "What personal budget and attendance constraints did I set?"
    memory = ConversationMemory.from_executor(store, "recall", executor)
    state = memory.catch_up()
    assert state.indexed_turns == 3 and state.pending_turns == 0
    items, recall = recall_stored_users(
        memory, state, conversation, turns, question, executor, set(), budget=1000
    )
    assert recall["status"] == "completed", recall
    actual_ids = {item["turn_id"] for item in items}
    assert {turns[i].turn_id for i in facts} <= actual_ids
    assert recall["context_tokens"] <= 1000
    assert recall["stage_sends"]["retrieval"] <= 8
    assert "ASSISTANT_POISON" not in json.dumps(model_service.requests)

    prepared = PreparedConversationInput(
        question, recalled=tuple(IntentTurn(**item) for item in items)
    )
    publish_pages(kb_dir, {"index.md": b"# Exhibition\nUser constraints are in supplied intent."})
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
