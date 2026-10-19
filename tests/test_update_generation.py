"""Large updates use real reads, one base snapshot and the ordinary model boundary."""

import json

import pytest
from compile_work_fixtures import accepted_payload
from test_page_link_capacity import model_send  # noqa: F401

pytest_plugins = ("compile_work_fixtures",)


@pytest.mark.asyncio
async def test_indivisible_evidence_capacity_fails_before_expensive_suffix_reads(
    kb_dir, monkeypatch
):
    from ultrakb.agent import compiler
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.page_updates import PageUpdate
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.agent.update_generation import generate_page_update

    wiki = kb_dir / "wiki"
    source = "A complete requirement with indivisible source metadata.\n" * 3
    (wiki / "sources/manual.md").write_text(source)
    page = wiki / "concepts/rules.md"
    page.write_text("---\ndescription: Rules\n---\n# Rules\n\nKeep the old rule.\n")
    reader = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    originals = reader.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(source)}"}},
        "concepts/rules.md",
        long=True,
    )
    before = page.read_bytes()
    projected_offsets, yielded_offsets = [], []
    project, iterate = reader._window, reader.iter_selection
    model_calls = []

    def observe_projection(path, location, budget):
        projected_offsets.append(location.get("offset", 0))
        return project(path, location, budget)

    def observe_selection(*args, **kwargs):
        for window in iterate(*args, **kwargs):
            yielded_offsets.append(window["selection_offset"])
            yield window

    async def unexpected_generation(*args, **kwargs):
        model_calls.append((args, kwargs))
        raise AssertionError("Capacity rejection must occur before model generation")

    # Observe real reader work after prepare. Do not alter any returned window,
    # continuation, capacity result, iteration count, or source bytes.
    monkeypatch.setattr(reader, "_window", observe_projection)
    monkeypatch.setattr(reader, "iter_selection", observe_selection)
    monkeypatch.setattr(compiler, "_llm_call_page_async", unexpected_generation)
    with pytest.raises(PageResponseError, match="page_update_capacity_exceeded"):
        await generate_page_update(
            patch=PageUpdate(page.read_text()),
            originals=originals,
            windows=reader,
            page_path=page,
            model="unused",
            bundle=None,
            system_message={"role": "system", "content": "Compile"},
            current_original=None,
            known_targets_message={"role": "user", "content": "rules"},
            task_message={"role": "user", "content": "Update the necessary rule"},
            # Indivisible metadata cannot fit even when the body is subdivided.
            fits=lambda messages: "selection_length" not in json.dumps(messages),
        )
    # Every attempted subdivision must stop at its first rejected window.
    # Inspect projections as well as yielded windows so eager work inside a
    # reader cannot be hidden by yielding only its first result.
    assert all(offset == 0 for offset in projected_offsets), {
        "projected_count": len(projected_offsets),
        "first_suffix": next(offset for offset in projected_offsets if offset > 0),
    }
    assert all(offset == 0 for offset in yielded_offsets), {
        "yielded_count": len(yielded_offsets),
        "first_suffix": next(offset for offset in yielded_offsets if offset > 0),
    }
    assert model_calls == []
    assert page.read_bytes() == before


@pytest.mark.parametrize("resume", [False, True])
def test_large_necessary_update_merges_two_proposals_without_overwriting(
    kb_dir, resume, compile_work
):
    from dataclasses import replace

    from publication_inputs import update_inputs
    from test_update_capacity_groups import _request_scope

    from ultrakb.agent.update_generation import prepare_update_requests
    from ultrakb.agent.update_proposals import UpdateProposal, merge_proposals
    from ultrakb.compile_checkpoints import digest

    original = "# Conditions\n\nOld winter.\n\nOld summer.\n"
    source = "Winter exception.\n\nSummer condition."
    page, reader, originals, patch, scoped, messages = update_inputs(
        kb_dir, original, source, name="conditions"
    )
    native = tuple(
        {**part, "source_role": "current"}
        for part in reader.iter_selection(
            "sources/manual.md", {"chars": f"0:{len(source)}"}, budget=20
        )
    )
    originals = replace(originals, windows=native)

    def fits(request):
        return sum(len(w["content"]) for w in _request_scope(request)[0]) <= 20

    requests = prepare_update_requests(
        patch, originals, reader, messages=messages, fits=fits, scoped=scoped
    )
    assert len(requests) == 2
    checkpoints = compile_work({"document": "manual"}, {"protocol": "page-edits"})
    proposals = [
        {
            "base_sha256": patch.digest,
            "edits": [{"block_id": block, "operation": "replace", "content": content}],
        }
        for block, content in (("b2", "Winter exception.\n"), ("b3", "Summer condition.\n"))
    ]
    for index, ((batch, editable), proposal) in enumerate(zip(requests, proposals, strict=True)):
        editable.apply(json.dumps(proposal), current=page.read_text())
        checkpoints.save(f"page:batch:{index}", digest(messages(batch, editable)), proposal)
        if resume and index == 0:
            assert page.read_text() == original
            checkpoints = compile_work({"document": "manual"}, {"protocol": "page-edits"})
    restored = [
        UpdateProposal(
            index, checkpoints.load(f"page:batch:{index}", digest(messages(batch, editable)))
        )
        for index, (batch, editable) in enumerate(requests)
    ]
    merged = merge_proposals(patch, restored)
    assert not merged.conflicts
    result = patch.apply(json.dumps(merged.response), current=page.read_text())
    assert result["content"] == "# Conditions\n\nWinter exception.\n\nSummer condition.\n"
    assert page.read_text() == original
    assert "".join(window["content"] for batch, _ in requests for window in batch) == source


def test_resolved_conflict_is_restored_without_new_sends(kb_dir, compile_work):
    from publication_inputs import update_inputs

    from ultrakb.agent.update_proposals import UpdateProposal, merge_proposals
    from ultrakb.compile_checkpoints import digest

    original = "# Conditions\n\nOld winter.\n\nUnchanged background.\n"
    source = "Winter exception.\n\nSummer exception."
    page, _, _, patch, _, _ = update_inputs(kb_dir, original, source, name="conditions")
    proposals = [
        UpdateProposal(
            index,
            {
                "base_sha256": patch.digest,
                "edits": [
                    {"block_id": "b2", "operation": "replace", "content": body + "\n"},
                ],
            },
        )
        for index, body in enumerate(("Winter exception.", "Summer exception."))
    ]
    merged = merge_proposals(patch, proposals)
    assert len(merged.conflicts) == 1
    conflict_input = {
        "source": source,
        "proposals": [p.response for p in proposals],
        "retained_edits": merged.response["edits"],
    }
    resolved = {
        "base_sha256": patch.digest,
        "edits": [
            {
                "block_id": "b2",
                "operation": "replace",
                "content": "Winter and summer exceptions.\n",
            },
        ],
    }
    patch.apply(json.dumps(resolved), current=page.read_text())
    checkpoints = compile_work({"document": "manual"}, {"protocol": "page-edits"})
    checkpoints.save("page:conflict:0", digest(conflict_input), resolved)
    before = accepted_payload(checkpoints, "page:conflict:0")
    restored = compile_work({"document": "manual"}, {"protocol": "page-edits"})
    result = patch.apply(
        json.dumps(restored.load("page:conflict:0", digest(conflict_input))),
        current=page.read_text(),
    )
    assert result["content"] == original.replace("Old winter.", "Winter and summer exceptions.")
    assert "Winter exception." in json.dumps(conflict_input) and "Summer exception." in json.dumps(
        conflict_input
    )
    assert accepted_payload(checkpoints, "page:conflict:0") == before
    assert page.read_text() == original


def test_old_page_partition_does_not_force_a_redundant_metadata_conflict(kb_dir):
    from publication_inputs import update_inputs
    from test_update_capacity_groups import _request_scope

    from ultrakb.agent.update_generation import prepare_update_requests
    from ultrakb.agent.update_proposals import UpdateProposal, merge_proposals

    old = "---\ndescription: Model A rules\n---\n# Rules\n\n" + "\n\n".join(
        f"Rule {index}: Keep this requirement." for index in range(4)
    )
    source = "All rules on this page apply to Models A and B. No body rule needs changing."
    page, reader, originals, patch, scoped, messages = update_inputs(kb_dir, old, source)
    requests = prepare_update_requests(
        patch,
        originals,
        reader,
        messages=messages,
        fits=lambda request: len(_request_scope(request)[1]) <= 2,
        scoped=scoped,
    )
    assert 1 < len(requests) < 10
    assert all(source in json.dumps(messages(batch, editable)) for batch, editable in requests)
    proposals = [
        UpdateProposal(
            index,
            {"base_sha256": patch.digest, "edits": [], "description": "Models A and B rules"},
            complete_scope=batch == list(originals.windows),
        )
        for index, (batch, _) in enumerate(requests)
    ]
    merged = merge_proposals(patch, proposals)
    assert not merged.conflicts
    result = patch.apply(json.dumps(merged.response), current=page.read_text())
    assert result["description"] == "Models A and B rules"
    assert page.read_text() == old


@pytest.mark.asyncio
async def test_short_update_partitions_complete_current_and_linked_originals_and_reuses_results(
    kb_dir,
    model_send,  # noqa: F811
):
    from ultrakb.agent.page_updates import PageUpdate, UnchangedPage
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.agent.update_generation import generate_page_update
    from ultrakb.locks import atomic_write_text

    wiki = kb_dir / "wiki"
    current = "\n\n".join(f"Rule {i}: retain its independent condition." for i in range(240))
    prior = "The prior source condition remains relevant.\n\nRetain its exception.\n"
    atomic_write_text(wiki / "sources/manual.md", current)
    atomic_write_text(wiki / "sources/prior.md", prior)
    page = wiki / "concepts/rules.md"
    old = "---\nsources: [sources/prior.md]\n---\n# Rules\n\nA prior rule.\n"
    atomic_write_text(page, old)
    reader = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    originals = reader.prepare({"name": "rules"}, "concepts/rules.md", long=False)
    patch = PageUpdate(old)
    observed = []

    def complete(messages):
        kwargs = {"messages": messages}
        assert len(json.dumps(kwargs["messages"], ensure_ascii=False).encode()) < 40000
        assert not any(
            isinstance(message["content"], str)
            and message["content"].startswith("Current original:")
            for message in kwargs["messages"]
        )
        rendered = next(
            block["text"]
            for message in kwargs["messages"]
            if isinstance(message["content"], list)
            for block in message["content"]
            if "Original source windows (bounded, frozen):\n" in block.get("text", "")
        )
        batch, _ = json.JSONDecoder().raw_decode(
            rendered.split("Original source windows (bounded, frozen):\n", 1)[1]
        )
        editable = next(
            message["content"].partition("\n")[2]
            for message in kwargs["messages"]
            if isinstance(message["content"], str)
            and message["content"].startswith("Temporary editable page blocks:\n")
        )
        observed.append((batch, json.JSONDecoder().raw_decode(editable)[0]["blocks"]))
        return json.dumps(
            {
                "status": "unchanged",
                "base_sha256": patch.digest,
                "reason": "The supplied conditions are already represented.",
            }
        )

    model_send.respond = complete
    options = dict(
        patch=patch,
        originals=originals,
        windows=reader,
        page_path=page,
        model="unused",
        bundle=None,
        system_message={"role": "system", "content": "Compile source facts."},
        current_original=reader.current_original_message(),
        known_targets_message={"role": "user", "content": "rules"},
        task_message={"role": "user", "content": "Update all necessary conditions as JSON."},
        fits=lambda messages: len(json.dumps(messages, ensure_ascii=False).encode()) < 40000,
    )
    with model_send.executor.activate():
        result = await generate_page_update(**options)
        assert isinstance(result, UnchangedPage)
        assert len(observed) > 1
        for block in patch.blocks:
            covered = [
                window
                for batch, blocks in observed
                if any(candidate["id"] == block for candidate in blocks)
                for window in batch
            ]
            for path, expected in (("sources/manual.md", current), ("sources/prior.md", prior)):
                assert "".join(w["content"] for w in covered if w["path"] == path) == expected
        before_resume = len(observed)
        await generate_page_update(**options)
        assert len(observed) == before_resume
    assert page.read_text() == old
