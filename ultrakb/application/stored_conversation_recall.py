"""Select bounded user intent through a real shared ConDB conversation tree."""

import hashlib
import json

from ultrakb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure
from ultrakb.context.recall import RECALL_VERSION, selected_user_excerpts
from ultrakb.llm_execution import StageBudgetExceeded, raise_if_terminal
from ultrakb.storage.session_store import SessionConflict
from ultrakb.storage.tree_store import TreeStore


def recall_stored_users(
    memory, state, conversation, turns, question, executor, recent_ids, *, budget
):
    measure = executor.question_capacity
    assert measure is not None
    if not state.tree_id or budget <= 0:
        return [], {"status": "unavailable", "context_tokens": 0, "coverage": "recent_only"}
    key = hashlib.sha256(
        json.dumps(
            {
                "database": memory.store.identity.__dict__,
                "session": conversation.session_id,
                "policy": state.policy_key,
                "prefix": state.prefix_digest,
                "tree": state.tree_id,
                "question": question,
                "retrieval": RECALL_VERSION,
                "tokenizer": measure.fingerprint,
                "roles": executor.fingerprint,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    refs = None
    before = executor.calls

    def check():
        executor.policy.check()
        memory.store.check_revision(conversation.session_id, conversation.revision)

    try:
        check()
        refs = executor.request_cache.get(key)
        if isinstance(refs, RetrievalFailure):
            raise refs
        cache_hit = refs is not None
        if refs is None:
            with TreeStore(memory.store.root, memory.store.identity).open(state.tree_id) as tree:
                result = tree.query(
                    question,
                    llm=ConDBLLMAdapter(executor, "conversation_retrieval"),
                    strategy="beam",
                    beam_size=3,
                    select_k=6,
                    max_turns=8,
                    cache_enabled=False,
                )
                refs = []
                for identity in result.node_ids:
                    node = tree.node(identity)
                    if node is None:
                        continue
                    attrs = node.get("attrs", {})
                    start, end = attrs.get("start_turn"), attrs.get("end_turn")
                    if (
                        attrs.get("kind") != "exchange"
                        or type(start) is not int
                        or type(end) is not int
                        or not 0 <= start < end <= state.indexed_turns
                        or end != start + 1
                        or attrs.get("turn_id") != turns[start].turn_id
                    ):
                        raise ValueError("Recall candidate is not a committed exchange")
                    original = turns[start]
                    refs.append(
                        {
                            "turn_id": original.turn_id,
                            "order": original.order,
                            "content_digest": original.content_digest,
                        }
                    )
            check()
            executor.request_cache[key] = refs
        check()
        found = selected_user_excerpts(
            turns[: state.indexed_turns], refs, recent_ids, measure, budget=budget
        )
        check()
        return found, {
            "status": "completed",
            "coverage": "selected_committed_users",
            "context_tokens": measure.payload_tokens(found) if found else 0,
            "turn_ids": [item["turn_id"] for item in found],
            "cache_hit": cache_hit,
            "stage_sends": executor.stage_calls,
            "notes": ["Unrecalled or unindexed history is not evidence of absence"],
        }
    except Exception as error:
        raise_if_terminal(error)
        if isinstance(error, (FileNotFoundError, SessionConflict)):
            raise
        if isinstance(error, RetrievalFailure) and refs is None:
            error.consumed_sends = executor.calls - before
            executor.request_cache[key] = error
        return [], {
            "status": "budget_exhausted" if isinstance(error, StageBudgetExceeded) else "degraded",
            "context_tokens": 0,
            "coverage": "recent_only",
            "reason_code": error.code
            if isinstance(error, RetrievalFailure)
            else "retrieval_failed",
            "stage_sends": executor.stage_calls,
        }
