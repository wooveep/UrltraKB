"""Word typography may vary in model quotes; evidence still binds to original characters."""

import json

import litellm
import pytest

from openkb.application.documents import import_document
from openkb.evidence import Evidence, ParseStore
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.mark.parametrize("space", ["\u00a0", "\u2007", "\u202f"])
def test_typographic_quote_space_preserves_original_text_and_offsets(
    kb_dir, tmp_path, monkeypatch, space
):
    original = f"dpkg{space}-i"
    source = tmp_path / "typography.md"
    source.write_text(f"1.ubuntu若直接{original}安装会有异常：", encoding="utf-8")
    calls, generated = [], []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            calls.append(payload)
            for unit in value["units"]:
                unit["facts"][0]["quote"] = "dpkg -i"
        if payload["stage"] == "generation":
            generated.extend(payload["facts"])
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert len(calls) == 1
    assert generated
    for fact in generated:
        assert fact["quote"] == original
        assert (
            ParseStore(kb_dir).read(Evidence(**fact["reference"]), max_chars=100).text == original
        )


@pytest.mark.parametrize(
    "text, quote",
    [
        ("dpkg\t-i", "dpkg -i"),
        ("dpkg  -i", "dpkg -i"),
        ("Limit is 37 kPa.", "Limit is 73 kPa."),
        ("Ａ requires version 7.", "A requires version 7."),
        ("dpkg\u00a0-i or dpkg\u202f-i", "dpkg -i"),
        ("Local evidence.", "Neighbor evidence."),
    ],
)
def test_quote_repair_does_not_accept_rewrites_or_ambiguous_positions(
    kb_dir, tmp_path, monkeypatch, text, quote
):
    source = tmp_path / "invalid.md"
    source.write_text(text, encoding="utf-8")
    events = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        for unit in value["units"]:
            unit["facts"][0]["quote"] = quote
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source, on_event=events.append)
    assert result.reason == "fact_evidence_invalid"
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    failures = [e for e in events if e.get("operation") == "response_invalid"]
    assert failures[-1]["field"] == "quote"
    assert failures[-1]["unit_id"]
    assert "quote" not in failures[-1] and "text" not in failures[-1]


def test_code_quotes_keep_strict_whitespace():
    from openkb.agent.evidence_quotes import fact_quote
    from openkb.agent.evidence_retry import ResponseIncomplete

    unit = {"id": "unit", "reference": {"block_id": "block"}, "kind": "code", "text": "x\u00a0= 1"}
    fact = {"topic": "Example", "statement": "An example", "quote": "x = 1"}
    with pytest.raises(ResponseIncomplete, match="fact_evidence_invalid"):
        fact_quote(unit, fact)
    fact["quote"] = unit["text"]
    assert fact_quote(unit, fact) == (unit["text"], 0, len(unit["text"]))


def test_previous_valid_fact_checkpoint_is_revalidated_and_reused(kb_dir, tmp_path, monkeypatch):
    from openkb.agent.evidence_checkpoints import CompilationCheckpoints

    source = tmp_path / "resume.md"
    source.write_text("Previously validated source facts.", encoding="utf-8")
    current_key = CompilationCheckpoints.key

    def previous_key(self, system, payload, **kwargs):
        if payload["stage"] == "facts":
            return self.previous_fact_key(system, payload)
        return current_key(self, system, payload, **kwargs)

    calls = []
    first_run = True

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls.append(payload["stage"])
        return response(
            evidence_response(payload), truncated=first_run and payload["stage"] == "planning"
        )

    monkeypatch.setattr(CompilationCheckpoints, "key", previous_key)
    monkeypatch.setattr(litellm, "completion", completion)
    first = import_document(kb_dir, source)
    assert first.reason == "output_budget_exhausted"
    assert calls.count("facts") == 1
    calls.clear()
    first_run = False
    monkeypatch.setattr(CompilationCheckpoints, "key", current_key)
    second = import_document(kb_dir, source)
    assert second.knowledge_compilation == "completed", second
    assert "facts" not in calls
