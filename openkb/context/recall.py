"""Locate conversation intent with ConDB, then reread committed user turns."""

from __future__ import annotations

import json

from contextdb import ConDB

from openkb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure
from openkb.context.conversation import ConversationMemory, MemoryState
from openkb.context.projection import fingerprint, open_runtime, selection_identity
from openkb.llm_execution import StageBudgetExceeded, raise_if_terminal

RECALL_VERSION = "openkb-user-intent-beam-v1"


def recall_users(
    memory: ConversationMemory,
    state: MemoryState,
    question: str,
    selection,
    recent_ids: set[str],
    *,
    budget: int = 2000,
) -> tuple[list[dict], dict]:
    executor = memory.executor
    measure = executor.question_capacity
    assert measure is not None
    if not state.tree_id or budget <= 0:
        return [], {"status": "unavailable", "context_tokens": 0, "coverage": "recent_only"}
    key, refs = None, None
    before = executor.calls
    try:
        memory.check_current()
        key = fingerprint(
            {
                "selection": selection_identity(selection),
                "policy": state.policy_key,
                "prefix": state.prefix_digest,
                "tree": state.tree_id,
                "question": question,
                "retrieval": RECALL_VERSION,
                "tokenizer": measure.fingerprint,
                "role_policy": executor.fingerprint,
            }
        )
        refs = executor.request_cache.get(key)
        if isinstance(refs, RetrievalFailure):
            raise refs
        cache_hit = refs is not None
        if refs is None:
            with open_runtime(memory.kb_dir) as db:
                result = ConDB(storage=db).query(
                    state.tree_id,
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
                    node = db.get_node(state.tree_id, identity)
                    if node is None:
                        continue
                    attrs = json.loads(node.attrs_json or "{}")
                    start, end = attrs.get("start_turn"), attrs.get("end_turn")
                    if (
                        type(start) is not int
                        or type(end) is not int
                        or not 0 <= start < end <= state.indexed_turns
                    ):
                        raise ValueError("Recall candidate is outside the committed prefix")
                    for turn in memory.session.turns[start:end]:
                        refs.append(
                            {
                                "turn_id": turn.turn_id,
                                "order": turn.order,
                                "content_digest": turn.content_digest,
                            }
                        )
            memory.check_current()
            executor.request_cache[key] = refs
        # Cache references, never generated topics or remembered text. Even a
        # repeated search validates the live authority and rereads user content.
        memory.check_current()
        by_id = {t.turn_id: t for t in memory.session.turns[: state.indexed_turns]}
        found: list[dict] = []
        seen = set(recent_ids)
        for reference in refs:
            original = by_id.get(reference["turn_id"])
            if (
                original is None
                or original.order != reference["order"]
                or original.content_digest != reference["content_digest"]
            ):
                raise ValueError("Recall candidate no longer matches the committed user turn")
            if original.turn_id in seen:
                continue
            seen.add(original.turn_id)
            item = {
                **reference,
                "text": original.user_text,
                "origin": "recalled_user",
                "char_start": 0,
                "char_end": len(original.user_text),
            }
            available = budget - measure.payload_tokens([*found, {**item, "text": ""}])
            if available <= 0:
                break
            item["text"] = measure.prefix(original.user_text, available)
            item["char_end"] = len(item["text"])
            # Escapes and locator framing count toward the recall allowance too.
            while item["text"] and measure.payload_tokens([*found, item]) > budget:
                item["text"] = item["text"][:-1]
                item["char_end"] = len(item["text"])
            if item["text"]:
                found.append(item)
        found.sort(key=lambda item: item["order"])
        memory.check_current()
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
        if isinstance(error, FileNotFoundError) or "changed during context" in str(error):
            raise
        if key and isinstance(error, RetrievalFailure) and refs is None:
            error.consumed_sends = executor.calls - before
            executor.request_cache[key] = error
        return [], {
            "status": "budget_exhausted" if isinstance(error, StageBudgetExceeded) else "degraded",
            "context_tokens": 0,
            "coverage": "recent_only",
            "reason_code": error.code
            if isinstance(error, RetrievalFailure)
            else (
                "retrieval_budget_exhausted"
                if isinstance(error, StageBudgetExceeded)
                else "retrieval_failed"
            ),
            "cache_hit": isinstance(refs, RetrievalFailure),
            "consumed_sends": error.consumed_sends
            if isinstance(error, RetrievalFailure)
            else executor.calls - before,
            "notes": [type(error).__name__, "Earlier user history has limited coverage"],
            "stage_sends": executor.stage_calls,
        }
