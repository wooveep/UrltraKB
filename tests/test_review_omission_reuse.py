"""Resolved unrelated gaps never reroll a bound, unchanged accepted review."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from openkb.agent import compiler
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.agent.evidence_verifier import verify_content
from openkb.config import DEFAULT_CONFIG
from openkb.execution_receipt import ModelText
from tests.test_generation_scopes import payload

REMAINING = {"stage": "facts", "block": "d" * 64, "reason": "evidence_output_invalid"}
RESOLVED = {"stage": "facts", "block": "e" * 64, "reason": "evidence_output_invalid"}


def checkpoints(kb_dir, settings, parse="c" * 64):
    return CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id="a" * 32, id="b" * 64),
        SimpleNamespace(id=parse),
        settings,
        None,
    )


@pytest.fixture
def reviews(monkeypatch):
    calls = []
    state = {"verdict": "supported"}
    settings = {
        **DEFAULT_CONFIG,
        "model": "openai/test",
        "compilation_thinking": "disabled",
        "verification_adjudication_thinking": "disabled",
        "processing": {**DEFAULT_CONFIG["processing"], "output_tokens": 4096},
    }

    def call(model, request, *args, **kwargs):
        p = json.loads(request[-1]["content"])
        calls.append(p)
        result = {"verdict": state["verdict"], "reason": "The same original conditions decide."}
        if state["verdict"] == "advisory":
            result["advisories"] = [
                {
                    "kind": "uncertainty",
                    "candidate": "Original instructions.",
                    "occurrences": [p["occurrences"][0]["id"]],
                    "reason": "A secondary label remains ambiguous.",
                }
            ]
        return ModelText(request.decode_response(json.dumps(result)), 4096)

    monkeypatch.setattr(compiler, "_llm_call", call)
    return calls, state, settings


def assess(
    kb_dir, settings, gaps, *, parse="c" * 64, evidence=None, content="Original instructions."
):
    p = payload()
    with checkpoints(kb_dir, settings, parse) as cp:
        return verify_content(
            "Task",
            content,
            p["facts"],
            evidence or p["evidence"],
            settings,
            checkpoints=cp,
            omission_context=gaps,
        )


@pytest.mark.parametrize("verdict", ["supported", "advisory", "unsupported", "uncertain"])
@pytest.mark.parametrize("change", ["remove", "reorder"])
@pytest.mark.parametrize("nested", [False, True])
def test_resume_changes_only_omissions(kb_dir, reviews, verdict, change, nested):
    calls, state, settings = reviews
    state["verdict"] = verdict

    def context(rows):
        return (
            {"omissions": rows, "source": [{"text": "Same operation context"}]} if nested else rows
        )

    first = assess(kb_dir, settings, context([REMAINING, RESOLVED]))
    gaps = [REMAINING] if change == "remove" else [RESOLVED, REMAINING]
    second = assess(kb_dir, settings, context(gaps))
    assert first == second
    expected = 1 if change == "reorder" or verdict in {"supported", "advisory"} else 2
    assert len(calls) == expected


@pytest.mark.parametrize(
    "change", ["gap_added", "gap_modified", "evidence", "content", "parse", "options"]
)
def test_changed_semantic_inputs_require_new_assessment(kb_dir, reviews, change):
    calls, _, settings = reviews
    assess(kb_dir, settings, [REMAINING, RESOLVED])
    gaps, kwargs = [REMAINING], {}
    if change == "gap_added":
        gaps.append({**RESOLVED, "block": "f" * 64})
    elif change == "gap_modified":
        gaps = [{**REMAINING, "reason": "different_missing_condition"}]
    elif change == "evidence":
        evidence = deepcopy(payload()["evidence"])
        evidence[0]["text"] += " Changed original restriction."
        kwargs["evidence"] = evidence
    elif change == "content":
        kwargs["content"] = "Changed instructions."
    elif change == "parse":
        kwargs["parse"] = "f" * 64
    else:
        settings = {**settings, "verification_thinking": "enabled"}
    assess(kb_dir, settings, gaps, **kwargs)
    assert len(calls) == 2


@pytest.mark.parametrize("damage", [None, "binding", "record"])
def test_upgrade_reuses_only_authenticated_legacy_supported_response(kb_dir, reviews, damage):
    calls, _, settings = reviews
    first = assess(kb_dir, settings, [REMAINING, RESOLVED])
    # Old releases wrote shared analysis + original raw recovery but had no gap index.
    # Removing recovery forces discovery from the source-bound immutable record.
    store = kb_dir / ".openkb/source-store"
    for path in (store / "compilation/recovery").glob("*-review.json"):
        path.unlink()
    # Recreate the pre-upgrade record shape, including its authentic content IDs.
    from openkb.sources import content_id

    for binding_path in (store / "analysis/bindings").rglob("*.json"):
        binding = json.loads(binding_path.read_text())
        old_path = store / "analysis/records" / (binding["analysis"] + ".json")
        record = json.loads(old_path.read_text())
        record["input"]["payload"].pop("omission_identity", None)
        record["id"] = content_id(record["input"])
        old_path.unlink()
        (old_path.parent / (record["id"] + ".json")).write_text(json.dumps(record))
        binding["analysis"] = record["id"]
        binding_path.unlink()
        (binding_path.parent / (content_id(binding) + ".json")).write_text(json.dumps(binding))
    if damage:
        paths = list(
            (store / "analysis" / ("bindings" if damage == "binding" else "records")).rglob(
                "*.json"
            )
        )
        assert paths
        path = paths[0]
        value = json.loads(path.read_text())
        value["source" if damage == "binding" else "digest"] = "damaged"
        path.write_text(json.dumps(value))
    second = assess(kb_dir, settings, [REMAINING])
    assert first == second
    assert len(calls) == (1 if damage is None else 2)


def test_batched_reviews_bind_gap_identities_across_restart(kb_dir, reviews, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    from openkb.agent.review_batching import review_batching

    calls, _, settings = reviews

    def call(model, request, *args, **kwargs):
        value = json.loads(request[-1]["content"])
        calls.append(value)
        review = {"verdict": "supported", "reason": "The candidate preserves the original."}
        result = (
            {"reviews": [{"id": row["id"], "review": review} for row in value["candidates"]]}
            if value["stage"] == "verification_batch"
            else review
        )
        return ModelText(request.decode_response(json.dumps(result)), 4096)

    monkeypatch.setattr(compiler, "_llm_call", call)

    def run(gaps):
        with checkpoints(kb_dir, settings) as cp, review_batching(), ThreadPoolExecutor(2) as pool:

            def one(title):
                p = payload()
                for row in p["evidence"]:
                    row["reference"] = {
                        "source_id": "a" * 32,
                        "version_id": "b" * 64,
                        "parse_id": "c" * 64,
                        "block_id": "1" * 64,
                    }
                return verify_content(
                    title,
                    "Original instructions.",
                    p["facts"],
                    p["evidence"],
                    settings,
                    checkpoints=cp,
                    omission_context=gaps,
                )

            pending = [
                pool.submit(copy_context().run, one, title) for title in ("Task A", "Task B")
            ]
            return [job.result() for job in pending]

    assess(kb_dir, settings, [REMAINING, RESOLVED])  # Initialize protocol and tokenizer caches.
    calls.clear()
    first = run([REMAINING, RESOLVED])
    assert run([REMAINING]) == first
    assert len(calls) == 1
    assert run([REMAINING, {**RESOLVED, "block": "f" * 64}]) == first
    assert len(calls) == 2, "A new original gap cannot collide with old transport labels."


@pytest.mark.parametrize("verdict", ["supported", "uncertain", "present_path"])
@pytest.mark.parametrize("changed_gap", [False, True])
def test_legacy_raw_only_uncertainty_blocks_without_reroll_or_false_binding(
    kb_dir, reviews, verdict, changed_gap
):
    from openkb.agent.evidence_units import messages
    from openkb.agent.evidence_verifier import verification_payload, verification_system
    from openkb.config import compilation_model_options

    calls, _, settings = reviews
    settings = {**settings, "verification_adjudication_thinking": "enabled"}
    p = payload()
    raw_payload = verification_payload(
        "Task",
        "Original instructions.",
        p["facts"],
        p["evidence"],
        omission_context=[REMAINING, RESOLVED],
    )
    raw = {"verdict": verdict, "reason": "The original condition remains unresolved."}
    if verdict == "present_path":
        raw.update(
            verdict="unsupported",
            issues=[
                {
                    "kind": "evidence_missing",
                    "candidate": "Original instructions.",
                    "occurrences": [raw_payload["occurrences"][0]["id"]],
                    "path": raw_payload["source_scopes"][0]["headings"],
                    "reason": "The prior parser retained this mistaken missing-path result.",
                }
            ],
        )
    with checkpoints(kb_dir, settings) as cp:
        key = cp.review_key(
            messages(verification_system(), raw_payload),
            settings["model"],
            compilation_model_options(settings, verification=True),
            0,
        )
        cp.save_recovery(key, "review", {"response": json.dumps(raw)})
    gaps = [REMAINING, {**RESOLVED, "block": "f" * 64}] if changed_gap else [REMAINING, RESOLVED]
    result = assess(kb_dir, settings, gaps)
    if verdict == "supported":
        assert len(calls) == 1, "Unbound old short-label support cannot authorize publication."
        assert not result.get("legacy_unbound")
    else:
        assert not calls, "Unbound uncertainty must not trigger path repair or adjudication."
        assert result["verdict"] == "uncertain" and result["legacy_unbound"]
        store = kb_dir / ".openkb/source-store"
        assert not list((store / "analysis/bindings").rglob("*.json"))
        assert len(list((store / "compilation/recovery").glob("*-review.json"))) == 1
