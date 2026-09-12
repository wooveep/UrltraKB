"""A rejected title is correction input, not a requested publication title."""

import json

from openkb.agent.evidence_generation_protocol import messages


def test_rejected_title_is_not_repeated_as_the_correction_target():
    payload = {
        "stage": "generation",
        "title": "Install and remove dsagent",
        "title_fixed": False,
        "revision": {
            "title": "Install and remove dsagent",
            "content": "rpm -e spice-vdagent",
            "reason": "The removal targets spice-vdagent, not dsagent.",
        },
    }
    wire = json.loads(messages("System", payload)[-1]["content"])
    assert "title" not in wire
    assert wire["revision"] == payload["revision"]
    assert "title_instruction" in wire
    assert payload["title"] == "Install and remove dsagent"


def test_uncorrected_and_previously_verified_titles_keep_the_existing_contract():
    for payload in (
        {"stage": "generation", "title": "Task", "title_fixed": False},
        {
            "stage": "generation",
            "title": "Task",
            "title_fixed": True,
            "revision": {"title": "Task", "content": "Body", "reason": "Fix body"},
        },
    ):
        wire = json.loads(messages("System", payload)[-1]["content"])
        assert wire["title"] == "Task"
        assert "title_instruction" not in wire


def test_deep_correction_is_explicit_and_does_not_change_normal_generation():
    from openkb.agent.evidence_generation_protocol import generation_options

    settings = {"compilation_thinking": "disabled", "correction_thinking": "enabled"}
    assert generation_options(settings)["extra_body"]["thinking"]["type"] == "disabled"
    assert (
        generation_options(settings, correction=True)["extra_body"]["thinking"]["type"] == "enabled"
    )
    assert (
        generation_options({"compilation_thinking": "disabled"}, correction=True)["extra_body"][
            "thinking"
        ]["type"]
        == "disabled"
    )


def test_title_repair_cannot_replace_scoped_facts_or_fragment_bindings():
    from copy import deepcopy

    from openkb.agent.evidence_generation_protocol import apply_title_correction, normalize_output
    from tests.test_generation_scopes import payload, response

    p = payload()
    original = normalize_output(response(p), p)
    p["revision"] = {
        "title": original["title"],
        "content": original["content"],
        "candidate": deepcopy(original),
        "reason": "Only title is unsupported.",
        "issues": [{"kind": "title", "candidate": original["title"]}],
    }
    malicious_rewrite = {
        "title": "Neutral topic",
        "content": "Invented body",
        "fragments": [],
        "covered": [],
    }
    fixed = normalize_output(apply_title_correction(malicious_rewrite, p), p)
    assert fixed["title"] == "Neutral topic"
    assert fixed["content"] == original["content"]
    assert fixed["fragments"] == original["fragments"]
    assert fixed["covered"] == original["covered"]
    assert p["revision"]["candidate"] == original
    wire = json.loads(messages("System", p)[-1]["content"])
    assert "candidate" not in wire["revision"]
    assert "Return only" in wire["output_contract"]


def test_mixed_body_issue_does_not_get_a_title_only_shortcut():
    from openkb.agent.evidence_generation_protocol import (
        apply_title_correction,
        title_only_correction,
    )

    p = {
        "stage": "generation",
        "title_fixed": False,
        "revision": {
            "title": "Wrong title",
            "content": "Wrong body",
            "issues": [
                {"kind": "title", "candidate": "Wrong title"},
                {"kind": "claim", "candidate": "Wrong body"},
            ],
        },
    }
    result = {"title": "Fixed title", "content": "Fixed body", "covered": ["f"]}
    assert not title_only_correction(p)
    assert apply_title_correction(result, p) == result
