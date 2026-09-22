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
        if payload["stage"] == "planning":
            calls.append(payload)
        if payload["stage"] == "generation":
            generated.extend(payload["evidence"]["blocks"])
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert len(calls) == 1
    assert generated
    for block in generated:
        assert original in block["text"]
        assert block["reference"]["parse_id"]
    from openkb.agent.evidence_units import source_units
    from openkb.config import resolve_effective_config
    from openkb.navigation import read_navigation
    from openkb.processing import RequestLimits
    from openkb.sources import SourceStore

    source_version = SourceStore(kb_dir).version(result.input_version)
    parsed = ParseStore(kb_dir).load(result.parse_id)
    settings = resolve_effective_config(kb_dir)[0]
    unit = next(
        source_units(
            kb_dir,
            source_version,
            parsed,
            RequestLimits.from_config(settings),
            settings["model"],
            navigation=read_navigation(kb_dir, source_version),
        )
    )
    from openkb.agent.evidence_facts import validate_unit

    facts = validate_unit(
        unit, {"facts": [{"topic": "Example", "statement": original, "quote": "dpkg -i"}]}
    )
    assert (
        ParseStore(kb_dir).read(Evidence(**facts[0]["reference"]), max_chars=100).text == original
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
def test_quote_repair_does_not_accept_rewrites_or_ambiguous_positions(text, quote):
    from openkb.agent.evidence_quotes import fact_quote
    from openkb.agent.evidence_retry import ResponseIncomplete

    unit = {"id": "unit", "reference": {"block_id": "block"}, "kind": "paragraph", "text": text}
    fact = {"topic": "Example", "statement": "Source requirement", "quote": quote}
    with pytest.raises(ResponseIncomplete, match="fact_evidence_invalid") as error:
        fact_quote(unit, fact)
    assert error.value.details["field"] == "quote"
    assert error.value.details["unit_id"] == "unit"


def test_code_quotes_keep_strict_whitespace():
    from openkb.agent.evidence_quotes import fact_quote
    from openkb.agent.evidence_retry import ResponseIncomplete

    unit = {"id": "unit", "reference": {"block_id": "block"}, "kind": "code", "text": "x\u00a0= 1"}
    fact = {"topic": "Example", "statement": "An example", "quote": "x = 1"}
    with pytest.raises(ResponseIncomplete, match="fact_evidence_invalid"):
        fact_quote(unit, fact)
    fact["quote"] = unit["text"]
    assert fact_quote(unit, fact) == (unit["text"], 0, len(unit["text"]))


def test_accepted_document_plan_is_reused_after_stop(kb_dir, tmp_path, monkeypatch):
    from openkb.application.execution import ExecutionContext
    from openkb.application.source_actions import continue_source
    from openkb.cancellation import OperationCancelled

    source = tmp_path / "resume.md"
    source.write_text("Previously planned source knowledge.", encoding="utf-8")

    calls = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls.append(payload["stage"])
        return response(evidence_response(payload))

    def stop_after_plan(event):
        if event.get("stage") == "planning" and event.get("status") == "accepted":
            raise OperationCancelled()

    monkeypatch.setattr(litellm, "completion", completion)
    first = import_document(kb_dir, source, context=ExecutionContext(on_event=stop_after_plan))
    assert first.knowledge_compilation == "stopped"
    assert calls == ["planning"]
    calls.clear()
    second = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert second.knowledge_compilation == "completed", second
    assert calls == ["generation", "verification"]
