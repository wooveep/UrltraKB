"""A separate issues object can be joined without discarding any review fields."""

import json

import pytest

from openkb.agent.evidence_verifier import _parse_review, verification_payload
from openkb.processing import ProcessingIncomplete
from tests.test_generation_scopes import payload


@pytest.mark.parametrize("verdict", ["supported", "unsupported"])
def test_separate_issues_object_preserves_the_exact_verdict_and_feedback(verdict):
    p = payload()
    request = verification_payload("Task", "No instructions.", p["facts"], p["evidence"])
    issue = {
        "kind": "claim",
        "candidate": "No instructions.",
        "occurrences": ["e1"],
        "reason": "Instructions are present in the source.",
    }
    first = {"verdict": verdict, "reason": "Review reason."}
    tail = {"issues": [issue] if verdict == "unsupported" else []}
    raw = json.dumps(first) + "\n" + json.dumps(tail)
    review = _parse_review(raw, request)
    assert review["verdict"] == verdict
    assert review["reason"] == first["reason"]
    assert review.get("issues", []) == tail["issues"]


@pytest.mark.parametrize(
    "tail",
    [
        '{"verdict":"unsupported"}',
        '{"reason":"Different reason","issues":[]}',
        '{"issues":[]}\n{"verdict":"unsupported"}',
        "Some prose",
        '{"issues":[{"kind":"claim","candidate":"No instructions.",'
        '"occurrences":["e1"],"reason":"Rejected"}]}',
    ],
)
def test_no_conflicting_or_discarded_tail_can_turn_into_support(tail):
    p = payload()
    request = verification_payload("Task", "No instructions.", p["facts"], p["evidence"])
    raw = '{"verdict":"supported","reason":"Source matches."}\n' + tail
    with pytest.raises(ProcessingIncomplete, match="evidence_verification_invalid"):
        _parse_review(raw, request)
