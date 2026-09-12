"""A usable located rejection must not be lost for lack of a repeated summary."""

import json

import pytest

from openkb.agent.evidence_verifier import verify_content
from openkb.processing import ProcessingIncomplete
from tests.test_generation_scopes import payload


@pytest.mark.parametrize("kind", ["scope", "claim", "title"])
def test_unmapped_candidate_claim_remains_an_explicit_rejection(kind):
    from openkb.agent.evidence_verifier import _parse_review, verification_payload

    p = payload()
    review = _parse_review(
        json.dumps(
            {
                "verdict": "unsupported",
                "issues": [
                    {
                        "kind": kind,
                        "candidate": "Invented step.",
                        "occurrences": [],
                        "reason": "No current source occurrence supports this additional claim.",
                    }
                ],
            }
        ),
        verification_payload("Invented step.", "Body.", p["facts"], p["evidence"]),
    )
    assert review["verdict"] == "unsupported"
    assert review["issues"][0]["occurrences"] == []


def test_located_rejection_without_summary_reaches_correction(monkeypatch):
    from openkb.agent import compiler

    calls = []
    issue = {
        "kind": "claim",
        "candidate": "No instructions.",
        "occurrences": ["e1"],
        "reason": "The original contains instructions; the candidate falsely says none.",
    }

    def call(*args, **kwargs):
        calls.append(args)
        return json.dumps({"verdict": "unsupported", "issues": [issue]})

    monkeypatch.setattr(compiler, "_llm_call", call)
    p = payload()
    result = verify_content(
        "Task",
        "No instructions.",
        p["facts"],
        p["evidence"],
        {"model": "test", "processing": {"max_attempts": 2}},
    )
    assert result == {"verdict": "unsupported", "reason": issue["reason"], "issues": [issue]}
    assert len(calls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        {"verdict": "supported", "issues": []},
        {"verdict": "unsupported", "issues": []},
        {
            "verdict": "unsupported",
            "issues": [
                {
                    "kind": "claim",
                    "candidate": "No instructions.",
                    "occurrences": ["invented"],
                    "reason": "Bad location",
                }
            ],
        },
    ],
)
def test_missing_summary_never_accepts_support_or_invalid_feedback(monkeypatch, reply):
    from openkb.agent import compiler

    monkeypatch.setattr(compiler, "_llm_call", lambda *args, **kwargs: json.dumps(reply))
    p = payload()
    with pytest.raises(ProcessingIncomplete, match="evidence_verification_invalid"):
        verify_content(
            "Task",
            "No instructions.",
            p["facts"],
            p["evidence"],
            {"model": "test", "processing": {"max_attempts": 1}},
        )
