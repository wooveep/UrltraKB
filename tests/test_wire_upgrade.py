"""A lifetime-only transport upgrade preserves completed semantic decisions."""

import json
import sys

import pytest

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response

PRE_CONTEXT_RELEASE = "45d9f407d6fc96b4cac7fb6a77b601c440cdf1cf2101b762c1a8038eef09e1d6"
pytestmark = pytest.mark.skipif(
    sys.version_info[:2] != (3, 12),
    reason="The preceding frozen artifact uses Python 3.12 bytecode.",
)


@pytest.mark.parametrize("change", ["module", "constant", "path"])
def test_context_upgrade_compatibility_requires_the_exact_module_and_implementation(
    monkeypatch, change
):
    from importlib.util import find_spec
    from types import SimpleNamespace

    from openkb import implementation

    name = "openkb.agent.evidence_wire"
    loader = find_spec(name).loader
    code = loader.get_code(name)
    assert implementation.module_revision(name) == PRE_CONTEXT_RELEASE
    if change == "module":
        name = "another_transport"
    else:
        source = loader.get_source(name)
        if change == "constant":
            source = source.replace("Expand it when reading evidence.", "Ignore that evidence.")
        code = compile(source, "C:/_MEI_TEST/openkb/agent/evidence_wire.pyc", "exec")
    monkeypatch.setattr(
        implementation,
        "find_spec",
        lambda name: SimpleNamespace(loader=SimpleNamespace(get_code=lambda name: code)),
    )
    implementation.module_revision.cache_clear()
    try:
        assert (implementation.module_revision(name) == PRE_CONTEXT_RELEASE) == (change == "path")
    finally:
        implementation.module_revision.cache_clear()


@pytest.mark.parametrize("verdict", ["supported", "unsupported", "uncertain"])
def test_context_lifetime_upgrade_preserves_decisions_until_rules_change(
    kb_dir, tmp_path, model_service, monkeypatch, verdict
):
    from openkb.agent import (
        evidence_checkpoints,
        evidence_dependencies,
        evidence_fact_cache,
        evidence_topic_cache,
        shared_analysis,
    )

    modules = (
        evidence_checkpoints,
        evidence_dependencies,
        evidence_fact_cache,
        evidence_topic_cache,
        shared_analysis,
    )
    current_revision = evidence_checkpoints.module_revision

    def previous_revision(name):
        return (
            PRE_CONTEXT_RELEASE if name == "openkb.agent.evidence_wire" else current_revision(name)
        )

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {"verdict": verdict, "reason": "The original condition decides this result."}
        return evidence_response(payload)

    model_service.respond = respond
    for module in modules:
        monkeypatch.setattr(module, "module_revision", previous_revision)
    source = tmp_path / "condition.md"
    source.write_text("# Recovery\n\nDisconnect power before replacing the controller.")
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    before = len(model_service)
    for module in modules:
        monkeypatch.setattr(module, "module_revision", current_revision)
    resumed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert resumed.knowledge_compilation == "completed", resumed
    assert len(model_service) == before, "An identical decision must not be sampled again."
    assert bool(list((kb_dir / "wiki/concepts").glob("*.md"))) == (verdict == "supported")
    assert bool(resumed.omissions) == (verdict != "supported")

    def changed_revision(name):
        return "f" * 64 if name == "openkb.agent.evidence_wire" else current_revision(name)

    from openkb.evidence import EVIDENCE_PROVENANCE

    monkeypatch.setitem(EVIDENCE_PROVENANCE, "context", "reader_context_with_explicit_role_origins")
    for module in modules:
        monkeypatch.setattr(module, "module_revision", changed_revision)
    changed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert changed.knowledge_compilation == "completed", changed
    stages = [
        json.loads(call["messages"][-1]["content"])["stage"] for call in model_service[before:]
    ]
    assert "facts" in stages and "generation" in stages and "verification" in stages
