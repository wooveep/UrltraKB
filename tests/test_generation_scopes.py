"""Source identity and output coverage at the real generation protocol boundary."""

from copy import deepcopy

import pytest

from openkb.agent.evidence_retry import ResponseIncomplete


def passages():
    parent = {"headings": ["GPU", "Compute"]}
    attached = {
        **parent,
        "attachment": {"part": "object2.bin", "name": "Driver.pdf", "position": {"page": 3}},
    }
    return [
        {"id": "license", "text": "LicenseServer", "location": attached},
        {"id": "commands", "text": "cp template config", "location": attached},
        {"id": "compute", "text": "Configure x.org", "location": parent},
        {"id": "displays", "text": "ConnectedMonitor", "location": {"headings": ["Dual screen"]}},
    ]


def payload(evidence=None):
    from openkb.agent.evidence_generation_protocol import generation_payload

    evidence = evidence or passages()
    facts = [
        {"id": p["id"], "quote": p["text"], "statement": p["text"], "reference": {}}
        for p in evidence
    ]
    return generation_payload(
        {"stage": "generation", "title": "GPU", "title_fixed": False}, facts, evidence
    )


def response(p):
    return {
        "title": "GPU configuration",
        "covered": [f["id"] for f in p["facts"]],
        "fragments": [
            {
                "scope": s["id"],
                "occurrences": s["occurrences"],
                "heading": f"Configuration {i}",
                "content": "Original command",
            }
            for i, s in enumerate(p["source_scopes"])
        ],
    }


def test_original_scopes_and_repeated_fact_windows_are_lossless():
    evidence = passages()
    evidence.insert(2, deepcopy(evidence[1]))
    before = deepcopy(evidence)
    p = payload(evidence)
    assert [len(s["occurrences"]) for s in p["source_scopes"]] == [3, 1, 1]
    assert len({o["id"] for o in p["occurrences"]}) == 5
    assert [o["fact_id"] for o in p["occurrences"]].count("commands") == 2
    assert p["source_scopes"][0]["origin"][0]["name"] == "Driver.pdf"
    assert p["source_scopes"][1]["origin"] == "enclosing_document"
    assert p["evidence"] == before == evidence


@pytest.mark.parametrize(
    "damage", ["missing", "repeat", "cross_scope", "unknown", "missing_covered"]
)
def test_invalid_fragment_mapping_cannot_become_a_contribution(damage):
    from openkb.agent.evidence_generation_protocol import normalize_output

    p = payload()
    out = response(p)
    if damage == "missing":
        out["fragments"].pop()
    elif damage == "repeat":
        out["fragments"].append(deepcopy(out["fragments"][0]))
    elif damage == "cross_scope":
        out["fragments"][0]["scope"] = out["fragments"][1]["scope"]
    elif damage == "unknown":
        out["fragments"][0]["occurrences"].append("invented")
    else:
        del out["covered"]
    with pytest.raises(ResponseIncomplete, match="topic_generation_incomplete"):
        normalize_output(out, p)


def test_fragment_assembly_preserves_code_headings_and_cache_integrity():
    from openkb.agent.evidence_generation_protocol import normalize_output

    p = payload()
    out = response(p)
    out["fragments"][0]["content"] = "# Local step\n\n```sh\n# a shell comment\n```"
    result = normalize_output(out, p)
    assert "### Local step" in result["content"]
    assert "```sh\n# a shell comment\n```" in result["content"]
    assert result == normalize_output(result, p)
    result["content"] = "Tampered content"
    with pytest.raises(ResponseIncomplete):
        normalize_output(result, p)


def test_generation_budget_uses_the_same_fragment_contract(monkeypatch):
    from openkb.agent import evidence_pages
    from openkb.agent.evidence_generation_protocol import messages

    seen = []
    monkeypatch.setattr(evidence_pages, "output_fits", lambda *args, **kwargs: True)
    monkeypatch.setattr(evidence_pages, "fits", lambda a, b, c, d: seen.append(d) or True)
    p = payload()
    assert evidence_pages._generation_fits(
        {"stage": "generation", "title": "GPU"}, p["facts"], p["evidence"], None, "test"
    )
    import json

    gen = [v for v in seen if v["stage"] == "generation"]
    assert len(gen) == 2
    for v in gen:
        assert v["source_scopes"] == p["source_scopes"]
        assert "fragments" in json.loads(messages("system", v)[-1]["content"])["output_contract"]


def test_large_catalog_does_not_fill_unrelated_prompt_space(monkeypatch):
    import litellm

    from openkb.agent.evidence_pages import _target_window
    from openkb.config import DEFAULT_PROCESSING
    from openkb.processing import RequestLimits

    measured = []
    monkeypatch.setattr(litellm, "token_counter", lambda **kw: measured.append(kw["text"]) or 10)
    targets = {f"concepts/unrelated-{i}" for i in range(4000)} | {"concepts/gpu-driver"}
    limits = RequestLimits.from_config(
        {"processing": {**DEFAULT_PROCESSING, "context_tokens": 32768, "output_tokens": 1024}}
    )
    selected = _target_window(targets, "GPU driver", "test-catalog", limits)
    assert selected == ["concepts/gpu-driver"]
    assert len(measured) <= 64


def test_full_evidence_window_is_checked_once_and_budget_splits_remain_lossless():
    from types import SimpleNamespace

    from openkb.agent import evidence_pages

    text = "X" * 1250 + "\n" + "X" * 1250
    reference = {
        "source_id": "0" * 32,
        "version_id": "1" * 64,
        "parse_id": "2" * 64,
        "block_id": "3" * 64,
        "start": 0,
        "end": len(text),
    }
    fact = {"id": "fact", "scope": reference}
    reader = SimpleNamespace(
        complete_bound=lambda reference: len(text),
        read=lambda ref, **kw: SimpleNamespace(
            text=text[ref.start : ref.end], context="", location={}
        ),
    )
    checks = []

    def fits(facts, evidence):
        checks.append(len(evidence[0]["text"]))
        return checks[-1] <= 3000

    result = list(evidence_pages._evidence_windows(fact, reader, {}, fits))
    assert [r["text"] for r in result] == [text]
    assert checks == [len(text)]
    result = list(
        evidence_pages._evidence_windows(
            fact, reader, {}, lambda facts, evidence: len(evidence[0]["text"]) <= 2048
        )
    )
    assert [len(r["text"]) for r in result] == [1251, 1250]
    assert "".join(r["text"] for r in result) == text
    assert result[0]["reference"]["end"] == result[1]["reference"]["start"]


def test_each_fragment_preserves_its_original_recovery_position():
    from openkb.agent.evidence_generation_protocol import normalize_output

    base = ["管理节点恢复", "超融合（2管理主备+n计算节点）", "必须重装"]
    p = payload(
        [
            {
                "id": "same",
                "text": "只需配置本主机。",
                "location": {"headings": [*base, "UUID不变", "管理节点主备"]},
            },
            {
                "id": "changed",
                "text": "只需配置本主机。",
                "location": {"headings": [*base, "UUID变更", "管理节点主备"]},
            },
        ]
    )
    p["language"] = "zh-cn"
    result = normalize_output(response(p), p)
    sections = result["content"].split("## Configuration ")[1:]
    assert len(sections) == 2
    for section in sections:
        assert "管理节点恢复" in section and "超融合" in section and "必须重装" in section
        assert "管理节点主备" in section
    assert "UUID不变" in sections[0] and "UUID变更" not in sections[0]
    assert "UUID变更" in sections[1] and "UUID不变" not in sections[1]
    assert normalize_output(result, p) == result


def test_single_scope_retains_context_without_changing_commands_or_heading():
    from openkb.agent.evidence_generation_protocol import normalize_output

    p = payload(
        [
            {
                "id": "one",
                "text": "restart --keep",
                "location": {"headings": ["Standby only", "Reinstallation"]},
            }
        ]
    )
    output = {
        "title": "Restart",
        "covered": ["one"],
        "content": "# Restart\n\n```sh\nrestart --keep\n```",
    }
    result = normalize_output(output, p)
    assert result["content"].startswith("# Restart\n")
    assert "Standby only" in result["content"] and "Reinstallation" in result["content"]
    assert "```sh\nrestart --keep\n```" in result["content"]
    assert normalize_output(result, p) == result


def test_parent_document_position_is_distinct_from_attachment_position():
    from openkb.agent.evidence_generation_protocol import normalize_output

    p = payload(
        [
            {
                "id": "child",
                "text": "Child instruction",
                "location": {
                    "headings": ["Parent installation"],
                    "attachment": {
                        "name": "Driver.pdf",
                        "part": "object1",
                        "position": {"headings": ["Child troubleshooting"]},
                    },
                },
            }
        ]
    )
    output = {"title": "Driver", "covered": ["child"], "content": "Child instruction"}
    result = normalize_output(output, p)["content"]
    assert "enclosing document: Parent installation" in result
    assert "attachment Driver.pdf: Child troubleshooting" in result
    assert "Parent installation › Child troubleshooting" not in result


def test_capacity_preflight_counts_application_retained_source_position(monkeypatch):
    from openkb.agent import evidence_pages

    evidence = [
        {
            "id": "one",
            "text": "Original command",
            "location": {"headings": ["Required scope " * 300]},
        }
    ]
    facts = [{"id": "one", "quote": "Original command", "reference": {}}]
    seen = []
    monkeypatch.setattr(evidence_pages, "output_fits", lambda *a, **k: True)

    def fits(limits, model, system, value):
        seen.append(value)
        return not (value.get("stage") == "verification" and "Required scope" in value["content"])

    monkeypatch.setattr(evidence_pages, "fits", fits)
    assert not evidence_pages._generation_fits(
        {"stage": "generation", "title": "Task"}, facts, evidence, None, "test"
    )
    assert any(row.get("stage") == "verification" for row in seen)


def test_capacity_preflight_does_not_validate_original_text_as_generated_markdown(monkeypatch):
    from openkb.agent import evidence_pages

    p = payload()
    p["evidence"][0]["text"] = "```sh\nrestart"
    monkeypatch.setattr(evidence_pages, "output_fits", lambda *a, **k: True)
    monkeypatch.setattr(evidence_pages, "fits", lambda *a, **k: True)
    assert evidence_pages._generation_fits(
        {"stage": "generation", "title": "Task"}, p["facts"], p["evidence"], None, "test"
    )
