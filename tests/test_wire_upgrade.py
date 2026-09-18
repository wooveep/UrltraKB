"""A lifetime-only transport upgrade preserves completed semantic decisions."""

import json
import sys

import pytest

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response

PRE_CONTEXT_RELEASE = "45d9f407d6fc96b4cac7fb6a77b601c440cdf1cf2101b762c1a8038eef09e1d6"
PREVIOUS_REVISIONS = {
    "openkb.agent.evidence_compiler": (
        "72d489daf2991201682539378c9c4f2494c88a84428db2224eb9ec7f70029687"
    ),
    "openkb.agent.evidence_wire": PRE_CONTEXT_RELEASE,
    "openkb.agent.evidence_verifier": (
        "44ffd30309c3d2e59ca98e9914a7b3816d2265f45f0e3695eed5791337dfe1a4"
    ),
    "openkb.agent.dependency_preflight": (
        "c343402ad5b592accbf78edf51c7555a8d6d57bb5b73291f52072b22ebdf6101"
    ),
    "openkb.agent.review_batching": (
        "24b864886597755448e55f5e96afb0225d33d88f136471dc01314ebd9316c356"
    ),
    "openkb.processing": "d7fccd55a14ce4e8ae8b178a30d7d78e08d39c781f9267ad4a2a5df8c5907123",
}
pytestmark = pytest.mark.skipif(
    sys.version_info[:2] != (3, 12),
    reason="The preceding frozen artifact uses Python 3.12 bytecode.",
)


@pytest.mark.parametrize("name", PREVIOUS_REVISIONS)
@pytest.mark.parametrize("change", ["module", "constant", "path"])
def test_context_upgrade_compatibility_requires_the_exact_module_and_implementation(
    monkeypatch, change, name
):
    from importlib.util import find_spec
    from types import SimpleNamespace

    from openkb import implementation

    previous = PREVIOUS_REVISIONS[name]
    loader = find_spec(name).loader
    code = loader.get_code(name)
    current = implementation.module_revision(name)
    if name == "openkb.agent.evidence_wire":
        # The frozen-prefix protocol changes wire semantics and cannot inherit
        # the earlier lifetime-only compatibility stamp.
        assert current != previous
    else:
        assert current == previous
    if change == "module":
        name = "another_transport"
    else:
        source = loader.get_source(name)
        if change == "constant":
            source += '\n_NEW_SEMANTIC_RULE = "Changed rules"\n'
        code = compile(source, "C:/_MEI_TEST/openkb/agent/evidence_wire.pyc", "exec")
    monkeypatch.setattr(
        implementation,
        "find_spec",
        lambda name: SimpleNamespace(loader=SimpleNamespace(get_code=lambda name: code)),
    )
    implementation.module_revision.cache_clear()
    try:
        if current != previous:
            assert implementation.module_revision(name) != previous
            assert (implementation.module_revision(name) == current) == (change != "constant")
        else:
            assert (implementation.module_revision(name) == previous) == (change == "path")
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
        return PREVIOUS_REVISIONS.get(name, current_revision(name))

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
