"""Prepare user intent from main-store originals and the accepted ChatIndex prefix."""

from dataclasses import asdict

from ultrakb.context.preparation import IntentTurn, PreparedConversationInput, prepare_recent_input
from ultrakb.conversation_turns import ConversationTurn, exchange_digest

from .conversation_memory import ConversationMemory


def user_turns(conversation):
    turns = []
    for turn in conversation.turns:
        messages = {message["role"]: message["content"] for message in turn["messages"]}
        turns.append(
            ConversationTurn(
                turn["turn_id"],
                turn["ordinal"] + 1,
                messages["user"],
                messages["assistant"],
                exchange_digest(messages["user"], messages["assistant"]),
                turn["turn_id"],
            )
        )
    return turns


def prepare_stored_conversation(store, conversation, question, executor, *, agent):
    turns = user_turns(conversation)
    prepared, allowance = prepare_recent_input(turns, question, executor, agent=agent)
    diagnostics = prepared.diagnostics
    recent_ids = {turn.turn_id for turn in prepared.recent}
    memory = ConversationMemory.from_executor(store, conversation.session_id, executor)
    planned = memory.prepare(
        committed_user_tokens=diagnostics["committed_user_tokens"],
        recall_budget=allowance,
        recent_turn_ids=recent_ids,
    )
    diagnostics["index"] = asdict(planned.state)
    diagnostics["recall_reason"] = planned.reason
    if planned.status == "recent_only":
        return prepared
    from .stored_conversation_recall import recall_stored_users

    found, recall = recall_stored_users(
        memory,
        planned.state,
        conversation,
        turns,
        question,
        executor,
        recent_ids,
        budget=allowance,
    )
    diagnostics["recall"] = recall
    diagnostics["status"] = (
        "prepared" if planned.status == "ready" and recall["status"] == "completed" else "limited"
    )
    return PreparedConversationInput(
        question, prepared.recent, tuple(IntentTurn(**item) for item in found), diagnostics
    )
