"""A pinned recovery-only change can reuse validated pages and unfinished drafts."""

from types import SimpleNamespace

from openkb.agent import evidence_checkpoints as checkpoints
from openkb.config import DEFAULT_CONFIG
from openkb.sources import content_id


def test_recovery_bridge_is_bound_to_exact_contract_and_inputs(kb_dir, monkeypatch):
    cp = checkpoints.CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id="a" * 32, id="b" * 64),
        SimpleNamespace(id="c" * 64),
        {**DEFAULT_CONFIG, "model": "openai/test"},
        None,
    )
    payload = {"stage": "generation", "title": "Task"}
    record = cp._key_record("System", payload, dependencies="d" * 64)
    record.pop("evidence_context")
    record.update(checkpoints._PREVIOUS_READERS)
    record["stage_implementation"] = dict(checkpoints._PREVIOUS_GENERATION)
    old = content_id(record)
    value = {"content": "Original", "covered": ["f1"], "_verification": {"verdict": "supported"}}
    cp.save(old, value)
    cp.save_recovery(old, "draft", {"output": value, "revision": None, "correction": 0})
    key = cp.key("System", payload, dependencies="d" * 64)
    assert key != old
    assert cp.load(key) == value
    assert cp.load_recovery(key, "draft")["output"] == value
    # Prefer the later checkpoint's correction state to the earlier draft.
    record["stage_implementation"]["evidence_verifier"] = (
        "f854f09cdf7c019455af8c4581f5947869b04a961d3104fb6829f1cd2def2ff3"
    )
    cp.save_recovery(
        content_id(record), "draft", {"output": None, "revision": "Reject", "correction": 1}
    )
    assert cp.load_recovery(key, "draft")["correction"] == 1
    assert cp.load(cp.key("System", payload, dependencies="e" * 64)) is None
    assert (
        cp.load(cp.key("System", {**payload, "evidence": ["expanded"]}, dependencies="d" * 64))
        is None
    )
    revision = checkpoints.module_revision
    monkeypatch.setattr(
        checkpoints,
        "module_revision",
        lambda name: "f" * 64 if name.endswith("evidence_verifier") else revision(name),
    )
    assert cp.load(cp.key("System", payload, dependencies="d" * 64)) is None
