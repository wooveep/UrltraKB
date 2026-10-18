"""Exercise accepted partial work through the real coordination/delivery seams.

Responses below are synthetic protocol fixtures, not a model quality evaluation.
The real validators, source readers and persistent checkpoints remain in use.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ultrakb.agent import compiler, update_generation
from ultrakb.agent.planning_identity import _compare
from ultrakb.compile_checkpoints import CompileCheckpoints


def saved_work(kb_dir):
    return CompileCheckpoints(
        kb_dir,
        SimpleNamespace(task_id="b" * 32, parent_task_id=None, compiler_request_version=3),
        {"document": "manual"},
        {"protocol": "coordination-regression"},
    )


def request_scope(messages):
    labels = (
        "Original source windows (bounded, frozen):\n",
        "Complete frozen originals for this page's current contributors:\n",
    )
    evidence = next(
        part["text"]
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if any(label in part.get("text", "") for label in labels)
    )
    label = next(label for label in labels if label in evidence)
    windows, _ = json.JSONDecoder().raw_decode(evidence.split(label, 1)[1])
    editable = next(
        message["content"].split("\n", 1)[1]
        for message in messages
        if isinstance(message.get("content"), str)
        and message["content"].startswith("Temporary editable page blocks:\n")
    )
    page, _ = json.JSONDecoder().raw_decode(editable)
    return windows, page["blocks"]


def test_coordination_subdivision_does_not_reopen_a_completed_source_prefix(kb_dir):
    from publication_inputs import update_inputs

    from ultrakb.agent.update_generation import prepare_update_requests

    source = "Independent source condition. " * 180
    _, reader, originals, patch, scoped, messages = update_inputs(
        kb_dir, "# Rules\n\nOld rule.\n", source
    )
    patch.allowed_blocks = frozenset({"b2"})
    native = list(
        reader.iter_selection("sources/manual.md", {"chars": f"0:{len(source)}"}, budget=1000)
    )
    subset = native[2:4]
    originals = replace(originals, windows=tuple(subset))
    requests = prepare_update_requests(
        patch,
        originals,
        reader,
        scoped=scoped,
        messages=messages,
        fits=lambda request: sum(len(w["content"]) for w in request_scope(request)[0]) <= 300,
    )
    covered = [w for batch, _ in requests for w in batch]
    assert "".join(w["content"] for w in covered) == "".join(w["content"] for w in subset)
    assert covered[0]["selection_offset"] == subset[0]["selection_offset"]
    assert covered[-1]["selection_offset"] + len(covered[-1]["content"]) == (
        subset[-1]["selection_offset"] + len(subset[-1]["content"])
    )


@pytest.mark.parametrize("reason_repeats", [34, 300])
@pytest.mark.parametrize("compact_rejection", [False, True])
def test_identity_coordination_reduces_verbose_receipts_and_resumes(
    kb_dir, reason_repeats, compact_rejection
):
    candidate = {"id": "candidate", "kind": "concepts", "name": "device", "brief": "Scope"}
    entry = {
        "id": "concepts/device",
        "kind": "concepts",
        "name": "device",
        "aliases": [],
        "description": "Model A only. Model B has a different identity. " * 150,
    }
    calls = []
    saved = saved_work(kb_dir)

    def fits(messages):
        return len(json.dumps(messages, ensure_ascii=False).encode()) <= 3500

    def send(messages, stage):
        assert fits(messages), "Every successor request must pass capacity admission"
        calls.append(messages)
        if "Compact identity reasoning" in str(messages):
            task = json.loads(messages[-1]["content"].split("\n", 1)[1])
            if compact_rejection and len(task["reason"]) > 100:
                from ultrakb.model_tokenizer import ContextCapacityError

                raise ContextCapacityError("Provider requires smaller reasoning fragments")
            return json.dumps({"reason": "Model A only; Model B differs."})
        target = json.loads(messages[-1]["content"].split("\n", 1)[1])["catalog"][0]
        return json.dumps(
            {
                "matches": [
                    {
                        "id": target["id"],
                        "relation": "different",
                        "action": "related",
                        "reason": "Model A only; Model B differs. " * reason_repeats,
                    }
                ]
            }
        )

    options = dict(
        system={"role": "system", "content": "Coordinate identities."},
        fits=fits,
        send=send,
        checkpoints=saved,
        identity={},
    )
    result = _compare(candidate, [entry], **options)
    assert len(result) == 1 and result[0]["relation"] == "different"
    assert any("Compact identity reasoning" in str(request) for request in calls)
    count = len(calls)
    assert _compare(candidate, [entry], **options) == result
    assert len(calls) == count, "Restart must reuse both fragment and coordination receipts"


@pytest.mark.asyncio
async def test_retained_disjoint_patches_compose_without_repeating_generation(kb_dir, monkeypatch):
    from publication_inputs import update_inputs

    page, reader, originals, patch, _, _ = update_inputs(
        kb_dir, "# Page\n\nAlpha.\n\nBeta.\n\nGamma.\n", "Alpha condition.\n\n    command\n"
    )
    source = (reader.wiki / "sources/manual.md").read_text()
    native = [
        {**window, "source_role": "current"}
        for location in ("0:16", f"18:{len(source)}")
        for window in reader.read_selection("sources/manual.md", {"chars": location})
    ]
    originals = replace(originals, message=originals.for_windows(native), windows=tuple(native))
    saved = saved_work(kb_dir)
    monkeypatch.setattr(update_generation, "document_checkpoints", lambda *args: saved)
    calls = []

    async def send(model, messages, stage, **kwargs):
        index = int(stage.rsplit(":", 1)[1])
        calls.append(index)
        block, text = (("b2", "Updated paragraph."), ("b3", "    command"))[index]
        return json.dumps(
            {
                "base_sha256": patch.digest,
                "edits": [{"block_id": block, "operation": "replace", "content": text}],
            }
        )

    monkeypatch.setattr(compiler, "_llm_call_async", send)

    options = dict(
        patch=patch,
        originals=originals,
        windows=reader,
        page_path=page,
        model="unused",
        bundle=None,
        current_original=None,
        system_message={"role": "system", "content": "Compile JSON."},
        known_targets_message={"role": "user", "content": "Known targets."},
        task_message={"role": "user", "content": "Update the supplied blocks."},
        fits=lambda request: sum(len(w["content"]) for w in request_scope(request)[0]) <= 16,
    )
    result = await update_generation.generate_page_update(**options)
    assert "Updated paragraph.\n\n    command" in result["content"]
    assert "Gamma.\n" in result["content"] and page.read_text() == patch.original
    assert calls == [0, 1]
    assert await update_generation.generate_page_update(**options) == result
    assert calls == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("current_original", [None, True])
@pytest.mark.parametrize(
    "scenario",
    [
        "normal",
        "interrupted",
        "fragmented",
        "provider",
        "step-provider",
        "body-conflict",
        "length",
        "short-tail",
    ],
)
async def test_global_metadata_and_range_conflicts_coordinate_in_bounded_steps(
    kb_dir, monkeypatch, current_original, scenario
):
    from publication_inputs import update_inputs

    source = "Winter condition. " * 140 + "\n\n" + "Summer condition. " * 140
    selected_end = len(source)
    if scenario == "short-tail":
        source += "\n\nTAIL_SENTINEL independent necessary scope."
    old = "---\ndescription: Winter rules\n---\n# Rules\n\nOld winter.\n\nOld summer.\n"
    page, reader, originals, patch, _, _ = update_inputs(kb_dir, old, source)
    middle = source.index("\n\n")
    native = [
        {**window, "source_role": "current"}
        for location in (f"0:{middle}", f"{middle + 2}:{selected_end}")
        for window in reader.read_selection("sources/manual.md", {"chars": location})
    ]
    originals = replace(originals, message=originals.for_windows(native), windows=tuple(native))
    patch.source_ranges = reader.page_figures(native, long=True).ids
    saved = saved_work(kb_dir)
    monkeypatch.setattr(update_generation, "document_checkpoints", lambda *args: saved)
    calls, seen, used_ranges, initial_ranges = [], set(), set(), set()
    stopped = False
    tail_seen = False
    save = saved.save

    def interrupt_after_accepted_step(key, *args, **kwargs):
        nonlocal stopped
        save(key, *args, **kwargs)
        if scenario == "interrupted" and ":steps-v1:" in key and not stopped:
            stopped = True
            raise OSError("Stopped after a durably accepted coordination step")

    monkeypatch.setattr(saved, "save", interrupt_after_accepted_step)

    def fits(messages):
        # Actual serialized size, plus a source budget that forces two initial batches.
        if scenario in {"provider", "length"} and "Resolve these conflicting proposals once" in str(
            messages
        ):
            return len(json.dumps(messages, ensure_ascii=False).encode()) <= 40000
        budget = (
            middle // 2
            if scenario == "fragmented" and ("Bounded update reconciliation" in str(messages))
            else middle
        )
        return (
            len(json.dumps(messages, ensure_ascii=False).encode())
            <= (40000 if scenario == "short-tail" else 16000)
            and sum(len(w["content"]) for w in request_scope(messages)[0]) <= budget
        )

    async def send(model, messages, stage, **kwargs):
        nonlocal tail_seen
        assert fits(messages)
        calls.append(stage)
        if scenario in {"provider", "length"} and "Resolve these conflicting proposals once" in str(
            messages
        ):
            from ultrakb.model_tokenizer import ContextCapacityError

            if scenario == "length":
                raise compiler.TruncatedResponseError("The full coordination output was truncated")
            raise ContextCapacityError("Provider rejected the full reconciliation request")
        batch, blocks = request_scope(messages)
        summer = any("Summer condition" in w["content"] for w in batch)
        label, target = ("Summer", "b3") if summer else ("Winter", "b2")
        if scenario == "body-conflict":
            target = "b2"
        bounded = "Bounded update reconciliation" in str(messages)
        if bounded and "TAIL_SENTINEL" in str(batch):
            tail_seen = True
        if bounded and scenario == "step-provider" and sum(len(w["content"]) for w in batch) > 1000:
            from ultrakb.model_tokenizer import ContextCapacityError

            raise ContextCapacityError("Provider requires finer source work in a bounded step")
        if bounded:
            seen.add((tuple(b["id"] for b in blocks), label))
        obj = {
            "base_sha256": patch.digest,
            "edits": [],
            "source_ranges": list(reader.page_figures(batch, long=True).ids),
        }
        if any(b["id"] == target for b in blocks):
            prior = ""
            if bounded and scenario == "body-conflict":
                control = next(
                    json.JSONDecoder().raw_decode(
                        m["content"].split("Bounded update reconciliation:\n", 1)[1]
                    )[0]
                    for m in messages
                    if isinstance(m.get("content"), str)
                    and "Bounded update reconciliation:\n" in m["content"]
                )
                prior = "".join(e["content"] for e in control["accumulated_draft"]["edits"])
            obj["edits"] = [
                {
                    "block_id": target,
                    "operation": "replace",
                    "content": prior + f"{label} condition.\n",
                }
            ]
        if not bounded or not blocks:
            obj["description"] = "Winter and summer rules" if summer else "Winter rules updated"
        elif not obj["edits"]:
            obj = {
                "status": "unchanged",
                "base_sha256": patch.digest,
                "reason": "Retain the accumulated contribution from the other source.",
            }
        if bounded and obj.get("status") != "unchanged":
            used_ranges.update(obj["source_ranges"])
        elif obj.get("status") != "unchanged":
            initial_ranges.update(obj["source_ranges"])
        return json.dumps(obj)

    monkeypatch.setattr(compiler, "_llm_call_async", send)
    options = dict(
        patch=patch,
        originals=originals,
        windows=reader,
        page_path=page,
        model="unused",
        bundle=None,
        current_original={"role": "user", "content": "Current original:\n" + source}
        if current_original
        else None,
        system_message={"role": "system", "content": "Compile JSON."},
        known_targets_message={"role": "user", "content": "Known targets."},
        task_message={"role": "user", "content": "Update all necessary conditions."},
        fits=fits,
    )
    if scenario == "interrupted":
        with pytest.raises(OSError, match="durably accepted"):
            await update_generation.generate_page_update(**options)
    result = await update_generation.generate_page_update(**options)
    assert result["description"] == "Winter and summer rules"
    assert "Winter condition." in result["content"] and "Summer condition." in result["content"]
    expected_ranges = used_ranges if scenario == "body-conflict" else used_ranges | initial_ranges
    assert set(result["source_ranges"]) == expected_ranges <= set(patch.source_ranges)
    assert {((), "Winter"), ((), "Summer")} <= seen
    if scenario == "short-tail" and current_original:
        assert tail_seen, (
            "Switching away from the dedicated short original cannot drop its unread tail"
        )
    if scenario == "body-conflict":
        assert {(("b2",), "Winter"), (("b2",), "Summer")} <= seen
    else:
        assert all(not blocks for blocks, _ in seen), (
            "Uncontested body edits must never be regenerated"
        )
    assert page.read_text() == old
    count = len(calls)
    assert await update_generation.generate_page_update(**options) == result
    assert len(calls) == count
    if scenario == "length":
        assert count == len(set(calls)) + 1, "Only one repair of the truncated parent is allowed"
    else:
        assert len(set(calls)) == count, (
            "Accepted steps and rejected global requests cannot be resent"
        )
