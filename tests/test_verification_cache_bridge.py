"""Bounded legacy drafts require fresh review and never inherit publication permission."""

from types import SimpleNamespace

from openkb.agent import evidence_checkpoints as checkpoints
from openkb.agent.legacy_checkpoints import generation_keys
from openkb.config import DEFAULT_CONFIG

TEST_CONFIG = {
    **DEFAULT_CONFIG,
    "model": "openai/test",
    "processing": {
        **DEFAULT_CONFIG["processing"],
        "context_tokens": 128_000,
        "max_context_tokens": 128_000,
        "max_output_tokens": 16_384,
    },
}


def test_recovery_bridge_is_bound_to_exact_contract_and_inputs(kb_dir, monkeypatch):
    cp = checkpoints.CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id="a" * 32, id="b" * 64),
        SimpleNamespace(id="c" * 64),
        TEST_CONFIG,
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
        lambda name, **kwargs: (
            "f" * 64 if name.endswith("document_pages") else revision(name, **kwargs)
        ),
    )
    changed = cp.key("System", payload, dependencies="d" * 64)
    assert changed != key
    assert cp.load(changed) is None


def test_verification_cache_keys_bind_the_actual_review_profile(kb_dir):
    cp = checkpoints.CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id="a" * 32, id="b" * 64),
        SimpleNamespace(id="c" * 64),
        TEST_CONFIG,
        None,
    )
    payload = {"stage": "verification", "candidate": "candidate-revision"}
    regular = cp.key("Review", payload, dependencies={"mode": "critical"})
    cp.verification_options = {"reasoning_effort": "high"}
    assert cp.key("Review", payload, dependencies={"mode": "critical"}) != regular

    adjudicated = cp.key("Review", payload, dependencies={"mode": "critical_adjudication"})
    cp.adjudication_options = {"reasoning_effort": "ultra"}
    assert cp.key("Review", payload, dependencies={"mode": "critical_adjudication"}) != adjudicated


def test_planning_cache_key_binds_ledger_projection_implementation(kb_dir, monkeypatch):
    cp = checkpoints.CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id="a" * 32, id="b" * 64),
        SimpleNamespace(id="c" * 64),
        TEST_CONFIG,
        None,
    )
    payload = {"stage": "planning", "target": {"target_start": 0, "target_end": 1}}
    original = cp.key("Plan", payload)
    revision = checkpoints.module_revision
    monkeypatch.setattr(
        checkpoints,
        "module_revision",
        lambda name, **kwargs: (
            "f" * 64
            if name == "openkb.agent.document_planning_ledger_views"
            else revision(name, **kwargs)
        ),
    )

    assert cp.key("Plan", payload) != original


def test_generation_cache_key_binds_normalized_candidate_reviewer(kb_dir, monkeypatch):
    cp = checkpoints.CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id="a" * 32, id="b" * 64),
        SimpleNamespace(id="c" * 64),
        TEST_CONFIG,
        None,
    )
    payload = {"stage": "generation", "title": "Task"}
    original = cp.key("Generate", payload, dependencies="d" * 64)
    revision = checkpoints.module_revision
    monkeypatch.setattr(
        checkpoints,
        "module_revision",
        lambda name, **kwargs: (
            "f" * 64
            if name == "openkb.agent.document_page_verification"
            else revision(name, **kwargs)
        ),
    )

    assert cp.key("Generate", payload, dependencies="d" * 64) != original


def test_generation_cache_key_excludes_review_only_profile(kb_dir):
    cp = checkpoints.CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id="a" * 32, id="b" * 64),
        SimpleNamespace(id="c" * 64),
        TEST_CONFIG,
        None,
    )
    payload = {"stage": "generation", "title": "Task"}
    regular = cp.key("Generate", payload, dependencies="d" * 64)
    cp.verification_options = {"reasoning_effort": "high"}
    cp.adjudication_options = {"reasoning_effort": "ultra"}
    assert cp.key("Generate", payload, dependencies="d" * 64) == regular
