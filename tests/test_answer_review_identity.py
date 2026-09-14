"""Lossless review identities keep original answers, evidence and history unchanged."""

import json

import pytest

from openkb.agent.chat_session import load_session
from openkb.application.conversations import continue_conversation
from openkb.locks import atomic_write_text
from tests.http_model_fixture import answer_review_response
from tests.test_answer_review_batches import _reader


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_support", [False, True])
async def test_review_identity_aliases_restore_exact_source_support_and_history(
    kb_dir, model_service, wrong_support
):
    source_id, version_id, parse_id, block_id = (
        c * n for c, n in [("a", 32), ("b", 64), ("c", 64), ("d", 64)]
    )
    target = f"sources/snapshots/{version_id}-{parse_id}.md#block-{block_id}"
    literal = "Literal source notation: ⟪id:1⟫."
    source = {
        "reference": {
            "source_id": source_id,
            "version_id": version_id,
            "parse_id": parse_id,
            "block_id": block_id,
        },
        "text": f"Receipt source: {source_id}. {literal}",
        "citation": f"[Source]({target})",
    }
    answer = f"Receipt source: {source_id} [Source]({target})"
    atomic_write_text(kb_dir / "wiki/sources/rows.md", json.dumps(source, ensure_ascii=False))
    model_service.chat_response = _reader(answer)
    model_service.chat_without_tools = True
    reviews = []

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        pool = payload.get("identity_pool", {})
        assert {source_id, version_id, parse_id, block_id} <= set(pool.values())
        compact = {k: v for k, v in payload.items() if k != "identity_pool"}
        assert all(identity not in json.dumps(compact) for identity in pool.values())
        observed = payload["observations"][0]["output"]
        assert literal in observed["text"]
        alias = next(key for key, value in pool.items() if value == source_id)
        assert alias in payload["answer"] and observed["reference"]["source_id"] == alias
        value = answer_review_response(payload)
        value["units"][0]["support"] = [
            {
                "observation": "o1",
                "path": ["reference", "source_id"],
                "value": next(key for key, item in pool.items() if item == version_id)
                if wrong_support
                else alias,
            }
        ]
        return {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)}

    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "Give the receipt's source identity and citation.")
    assert (result.status == "completed") == (not wrong_support), result
    assert len(reviews) == (2 if wrong_support else 1)
    if not wrong_support:
        assert result.answer == answer
        saved = load_session(kb_dir, result.session_id)
        assert saved.assistant_texts == [answer]
        assert not any("identity_pool" in str(row) for row in saved.history)
