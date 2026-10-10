"""Large updates use real reads, one base snapshot and the ordinary model boundary."""

import json

import pytest
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_large_necessary_update_merges_two_proposals_without_overwriting(
    kb_dir, model_service, resume
):
    from openkb.agent.page_updates import PageUpdate
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.agent.update_generation import generate_page_update
    from openkb.application.execution import ExecutionContext
    from openkb.locks import LockCancelled, kb_ingest_lock

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n"
        "  retries: 0\n  max_calls: 2\n  compile_repair_calls: 0\n"
    )
    wiki = kb_dir / "wiki"
    original = "# Conditions\n\nOld winter.\n\nOld summer.\n"
    page = wiki / "concepts/conditions.md"
    page.write_text(original)
    (wiki / "sources/manual.md").write_text(
        "Winter: " + "w" * 16000 + "\n\nSummer: " + "s" * 16000 + "\n"
    )
    windows = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    originals = windows.prepare(
        {"name": "conditions", "reading_locations": {"chars": "0:32019"}},
        "concepts/conditions.md",
        long=True,
    )
    patch = PageUpdate(original)
    for block, text in [("b2", "Winter exception.\n"), ("b3", "Summer condition.\n")]:
        model_service.replies.append(
            (
                200,
                response(
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "base_sha256": patch.digest,
                                "edits": [
                                    {"block_id": block, "operation": "replace", "content": text}
                                ],
                            }
                        ),
                    }
                ),
            )
        )

    async def generate():
        return await generate_page_update(
            patch=patch,
            originals=originals,
            windows=windows,
            page_path=page,
            model="ignored",
            bundle=None,
            system_message={"role": "system", "content": "Compile"},
            current_original=None,
            known_targets_message={"role": "user", "content": "Conditions"},
            task_message={"role": "user", "content": "Update all required conditions."},
            fits=lambda messages: len(json.dumps(messages)) < 25000,
        )

    def paused_after_first():
        return any(
            entry.get("state") == "accepted"
            for p in (kb_dir / ".openkb/compile-checkpoints").rglob("manifest.json")
            for entry in json.loads(p.read_text())["entries"].values()
        )

    context = ExecutionContext(cancelled=paused_after_first if resume else lambda: False)
    with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir):
        if resume:
            with pytest.raises(LockCancelled):
                await generate()
        else:
            result = await generate()
    if resume:
        assert context.executor.calls == len(model_service.requests) == 1
        assert page.read_text() == original
        context = ExecutionContext(resume_model_task_id=context.model_task.record.task_id)
        with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir):
            result = await generate()
    assert result["content"] == "# Conditions\n\nWinter exception.\n\nSummer condition.\n"
    assert page.read_text() == original  # The caller publishes exactly once after all results.
    assert len(model_service.requests) == context.executor.calls == 2


@pytest.mark.asyncio
async def test_resolved_conflict_is_restored_without_new_sends(kb_dir, model_service):
    from openkb.agent.page_updates import PageUpdate
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.agent.update_generation import generate_page_update
    from openkb.application.execution import ExecutionContext
    from openkb.locks import LockCancelled, kb_ingest_lock

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n"
        "  retries: 0\n  max_calls: 3\n  compile_repair_calls: 0\n"
    )
    wiki = kb_dir / "wiki"
    original = "# Conditions\n\nOld winter.\n\n" + "Unchanged background. " * 500 + "\n"
    page = wiki / "concepts/conditions.md"
    page.write_text(original)
    source = "Winter: " + "w" * 16000 + "\n\nSummer: " + "s" * 16000 + "\n"
    (wiki / "sources/manual.md").write_text(source)
    windows = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    originals = windows.prepare(
        {"name": "conditions", "reading_locations": {"chars": f"0:{len(source)}"}},
        "concepts/conditions.md",
        long=True,
    )
    patch = PageUpdate(original)
    for body in ["Winter exception.", "Summer exception.", "Winter and summer exceptions."]:
        model_service.replies.append(
            (
                200,
                response(
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "base_sha256": patch.digest,
                                "edits": [
                                    {
                                        "block_id": "b2",
                                        "operation": "replace",
                                        "content": body + "\n",
                                    }
                                ],
                            }
                        ),
                    }
                ),
            )
        )

    async def generate():
        return await generate_page_update(
            patch=patch,
            originals=originals,
            windows=windows,
            page_path=page,
            model="ignored",
            bundle=None,
            system_message={"role": "system", "content": "Compile"},
            current_original=None,
            known_targets_message={"role": "user", "content": "Conditions"},
            task_message={"role": "user", "content": "Update all required conditions."},
            fits=lambda messages: len(json.dumps(messages)) < 40000,
        )

    def stopped():
        return any(
            ":conflict:" in key and entry.get("state") == "accepted"
            for p in (kb_dir / ".openkb/compile-checkpoints").rglob("manifest.json")
            for key, entry in json.loads(p.read_text())["entries"].items()
        )

    first = ExecutionContext(cancelled=stopped)
    with kb_ingest_lock(kb_dir / ".openkb"), first.begin(kb_dir), pytest.raises(LockCancelled):
        await generate()
    assert len(model_service.requests) == first.executor.calls == 3
    assert page.read_text() == original
    resumed = ExecutionContext(resume_model_task_id=first.model_task.record.task_id)
    with kb_ingest_lock(kb_dir / ".openkb"), resumed.begin(kb_dir):
        result = await generate()
    assert len(model_service.requests) == resumed.executor.calls == 3
    assert result["content"] == original.replace("Old winter.", "Winter and summer exceptions.")
    final_request = json.dumps(model_service.requests[-1])
    assert "Winter exception." in final_request and "Summer exception." in final_request
    assert page.read_text() == original
