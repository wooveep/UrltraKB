"""Resume the same recorded review after a parser repair, without rerolling it."""

import json
from types import SimpleNamespace

import pytest

from openkb.agent import compiler
from openkb.agent import evidence_verifier as verifier
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.config import DEFAULT_CONFIG
from openkb.processing import ProcessingIncomplete
from tests.test_generation_scopes import payload


def test_repaired_review_parser_preserves_the_recorded_rejection(kb_dir, monkeypatch):
    settings = {
        **DEFAULT_CONFIG,
        "model": "openai/test",
        "compilation_thinking": "disabled",
        "verification_adjudication_thinking": "enabled",
    }
    cp = CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id="a" * 32, id="b" * 64),
        SimpleNamespace(id="c" * 64),
        settings,
        None,
    )
    issue = {
        "kind": "claim",
        "candidate": "No instructions.",
        "occurrences": ["e1"],
        "reason": "The original contains instructions.",
    }
    calls = []

    def call(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return json.dumps(
                {"verdict": "unsupported", "reason": issue["reason"], "issues": [issue]}
            )
        if len(calls) == 2:
            return json.dumps({"verdict": "unsupported", "issues": [issue]})
        return json.dumps({"verdict": "supported", "reason": "Different stochastic response"})

    monkeypatch.setattr(compiler, "_llm_call", call)
    parse = verifier._parse_review

    def old_parser(raw, request):
        if "reason" not in json.loads(raw):
            raise ProcessingIncomplete("evidence_verification_invalid", "generation")
        return parse(raw, request)

    monkeypatch.setattr(verifier, "_parse_review", old_parser)
    p = payload()
    args = ("Task", "No instructions.", p["facts"], p["evidence"], settings)
    with pytest.raises(ProcessingIncomplete, match="evidence_verification_invalid"):
        verifier.verify_content(*args, checkpoints=cp)
    assert len(calls) == 2
    monkeypatch.setattr(verifier, "_parse_review", parse)
    result = verifier.verify_content(*args, checkpoints=cp)
    assert result["verdict"] == "unsupported"
    assert len(calls) == 2
    result = verifier.verify_content(
        "Task", "Corrected instructions.", p["facts"], p["evidence"], settings, checkpoints=cp
    )
    assert result["verdict"] == "supported"
    assert len(calls) == 3


def test_separate_issue_object_recovers_recorded_adjudication_without_reroll(kb_dir, monkeypatch):
    settings = {
        **DEFAULT_CONFIG,
        "model": "openai/test",
        "compilation_thinking": "disabled",
        "verification_adjudication_thinking": "enabled",
    }
    cp = CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id="a" * 32, id="b" * 64),
        SimpleNamespace(id="c" * 64),
        settings,
        None,
    )
    calls = []

    def call(*args, **kwargs):
        calls.append(args)
        assert len(calls) <= 2, "Recorded adjudication must not be rerolled"
        if len(calls) == 1:
            return '{"verdict":"unsupported","reason":"Check the mode requirement."}'
        return '{"verdict":"supported","reason":"The original matches."}\n{"issues":[]}'

    parse = verifier._parse_review

    def old_parser(raw, request):
        try:
            json.loads(raw)
        except ValueError:
            raise ProcessingIncomplete("evidence_verification_invalid", "generation") from None
        return parse(raw, request)

    monkeypatch.setattr(compiler, "_llm_call", call)
    monkeypatch.setattr(verifier, "_parse_review", old_parser)
    p = payload()
    args = ("Task", "Original instructions.", p["facts"], p["evidence"], settings)
    with pytest.raises(ProcessingIncomplete, match="evidence_verification_invalid"):
        verifier.verify_content(*args, checkpoints=cp)
    assert len(calls) == 2
    monkeypatch.setattr(verifier, "_parse_review", parse)
    result = verifier.verify_content(*args, checkpoints=cp)
    assert result["verdict"] == "supported"
    assert len(calls) == 2
