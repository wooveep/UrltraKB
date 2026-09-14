"""Located review identities remain binding across ambiguous prose and further recovery."""

import json

import pytest

from openkb.application.conversations import ask_question
from openkb.locks import atomic_write_text
from tests.http_model_fixture import answer_review_response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["repeated_claim", "cross_unit", "bad_citation", "citation_escape"]
)
async def test_supported_units_stay_frozen_through_all_correction_steps(
    kb_dir, model_service, case
):
    fixed = "A permits public access."
    bad_tail = (
        "B permits public access." if case == "repeated_claim" else "B uses 37.\nOnly standby."
    )
    good_tail = (
        "B permits local access." if case == "repeated_claim" else "B uses 38.\nOnly active."
    )
    bad, good = fixed + "\n" + bad_tail, fixed + "\n" + good_tail
    atomic_write_text(kb_dir / "wiki/sources/scope.md", good)
    corrections, reviews = [], []

    def chat(body):
        try:
            payload = json.loads(body["messages"][-1]["content"])
        except ValueError:
            payload = {}
        if payload.get("stage") == "answer_correction":
            corrections.append(payload)
            assert "u1" not in payload["editable_units"]
            assert payload["answer"] == bad  # Even citation recovery retains the original scope.
            expected = ["u2"] if case == "repeated_claim" else ["u2", "u3"]
            assert payload["editable_units"] == expected
            edits = [
                {"unit": identity, "text": text}
                for identity, text in zip(expected, good_tail.splitlines())
            ]
            if case in {"bad_citation", "citation_escape"} and len(corrections) == 1:
                edits[-1]["text"] += " [Original](sources/not-observed.md)"
            if case == "citation_escape" and len(corrections) == 2:
                edits.append({"unit": "u1", "text": "A permits local access."})
            return {"role": "assistant", "content": json.dumps({"edits": edits, "insertions": []})}
        if any(row["role"] == "tool" for row in body["messages"]):
            return {"role": "assistant", "content": bad}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "original",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"sources/scope.md"}'},
                }
            ],
        }

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload["answer"])
        if payload["answer"] == good:
            return {"role": "assistant", "content": json.dumps(answer_review_response(payload))}
        claim = "public access" if case == "repeated_claim" else bad_tail
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "verdict": "unsupported",
                    "issues": [
                        {
                            "kind": "unsupported",
                            "claim": claim,
                            "reason": "Use the literal source values.",
                        }
                    ],
                    "units": [
                        {
                            "id": row["id"],
                            "verdict": "supported" if row["id"] == "u1" else "unsupported",
                            "support": [{"observation": "o1", "quote": good}],
                        }
                        for row in payload["units"]
                    ],
                }
            ),
        }

    model_service.chat_response = chat
    model_service.answer_review_response = review
    model_service.chat_without_tools = True
    result = await ask_question(kb_dir, "Report A and B literally.", save=True)
    assert len(corrections) == (2 if case in {"bad_citation", "citation_escape"} else 1)
    if case == "citation_escape":
        assert result.status != "completed" and result.saved_path is None
        assert reviews == [bad]
    else:
        assert result.status == "completed", result
        assert result.answer == good
        assert reviews == [bad, good]
