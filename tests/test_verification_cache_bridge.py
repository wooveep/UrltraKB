"""Bounded legacy drafts require fresh review and never inherit publication permission."""

from types import SimpleNamespace

from openkb.agent import evidence_checkpoints as checkpoints
from openkb.agent.legacy_checkpoints import generation_keys
from openkb.config import DEFAULT_CONFIG


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
    historical = generation_keys(record)
    old = historical[-1]
    value = {"content": "Original", "covered": ["f1"], "_verification": {"verdict": "supported"}}
    cp.save(old, value)
    cp.save_recovery(old, "draft", {"output": value, "revision": None, "correction": 0})
    key = cp.key("System", payload, dependencies="d" * 64)
    assert key != old
    assert cp.load(key) is None
    assert cp.load_recovery(key, "draft")["output"] == {"content": "Original", "covered": ["f1"]}
    assert cp.load(old) == value  # Immutable historical receipt remains intact.
    # Prefer the later checkpoint's correction state to the earlier draft.
    cp.save_recovery(
        historical[-2], "draft", {"output": None, "revision": "Reject", "correction": 1}
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
