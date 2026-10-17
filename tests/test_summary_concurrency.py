"""Controlled completion order proves scheduling, not model semantic quality."""

import asyncio
import json

import pytest

from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, build_document_summary
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.locks import atomic_write_text


def original_windows(kb):
    original = "\n\n".join(
        f"Section {i}. " + "A complete original condition. " * 3 for i in range(4)
    )
    atomic_write_text(kb / "wiki/sources/manual.md", original)
    atomic_write_text(
        kb / "wiki/summaries/manual.md", "---\nfull_text: sources/manual.md\n---\nOld."
    )
    return CompileSourceWindows(kb / "wiki", "manual", navigation=[])


@pytest.mark.asyncio
@pytest.mark.integration
async def test_summary_windows_overlap_but_assemble_in_original_order(kb_dir):
    windows = original_windows(kb_dir)
    ready = asyncio.Event()
    active = peak = 0
    finished = []

    async def generate(messages, stage, originals, sections):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        key = stage.rsplit(":", 1)[1]
        if key == "1":
            await asyncio.wait_for(ready.wait(), timeout=1)
            await asyncio.sleep(0.01)
        else:
            ready.set()
        finished.append(key)
        active -= 1
        return {
            "description": key,
            "sections": [
                {"id": section, "content": f"Accepted original window {key}."}
                for section in sections
            ],
        }

    draft = await build_document_summary(
        windows,
        system_message={"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")},
        generate=generate,
        fits=lambda messages: len(json.loads(messages[1]["content"].split("\n", 1)[1])["content"])
        <= 120,
        max_concurrency=2,
    )
    assert peak == 2
    assert finished[0] != "1"
    assert draft.markdown.index("window 1.") < draft.markdown.index("window 2.")
    assert len(draft.ranges) == len(finished)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_failed_window_settles_sibling_and_resume_only_sends_missing_work(kb_dir):
    from types import SimpleNamespace

    from ultrakb.compile_checkpoints import CompileCheckpoints

    windows = original_windows(kb_dir)
    task = SimpleNamespace(task_id="a" * 32, parent_task_id=None)
    checkpoints = CompileCheckpoints(kb_dir, task, {"document": "manual"}, {})
    entered = asyncio.Event()
    sent = []

    async def generate(messages, stage, originals, sections):
        key = stage.rsplit(":", 1)[1]
        sent.append(key)
        if key == "1":
            await asyncio.wait_for(entered.wait(), 1)
            raise ConnectionError("Interrupted first window")
        entered.set()
        await asyncio.sleep(0.01)
        return {
            "description": key,
            "sections": [
                {"id": section, "content": f"Accepted original window {key}."}
                for section in sections
            ],
        }

    options = dict(
        system_message={"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")},
        fits=lambda messages: len(json.loads(messages[1]["content"].split("\n", 1)[1])["content"])
        <= 120,
        checkpoints=checkpoints,
        max_concurrency=2,
    )
    with pytest.raises(ConnectionError):
        await build_document_summary(windows, generate=generate, **options)
    assert sent == ["1", "2"]
    sent.clear()

    async def resume(messages, stage, originals, sections):
        key = stage.rsplit(":", 1)[1]
        sent.append(key)
        return {
            "description": key,
            "sections": [
                {"id": section, "content": f"Accepted original window {key}."}
                for section in sections
            ],
        }

    draft = await build_document_summary(windows, generate=resume, **options)
    assert "2" not in sent and "1" in sent
    assert "window 2." in draft.markdown
    assert len(draft.ranges) == len(sent) + 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_split_windows_share_the_queue_and_cached_results_survive_serial_fallback(kb_dir):
    from types import SimpleNamespace

    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import CompileCheckpoints

    windows = original_windows(kb_dir)
    task = SimpleNamespace(task_id="c" * 32, parent_task_id=None, compiler_request_version=3)
    checkpoints = CompileCheckpoints(kb_dir, task, {"document": "manual"}, {})
    sent = []
    active = peak = 0

    async def generate(messages, stage, originals, sections):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        key = stage.rsplit(":", 1)[1]
        sent.append(key)
        try:
            await asyncio.sleep(0.01)
            if key == "1":
                raise PageResponseError("page_truncated_response", "Truncated parent")
            return {
                "description": key,
                "sections": [
                    {"id": section, "content": f"Accepted original window {key}."}
                    for section in sections
                ],
            }
        finally:
            active -= 1

    options = dict(
        system_message={"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")},
        fits=lambda messages: len(json.loads(messages[1]["content"].split("\n", 1)[1])["content"])
        <= 240,
        checkpoints=checkpoints,
    )
    draft = await build_document_summary(windows, generate=generate, max_concurrency=2, **options)
    assert peak == 2
    assert {"1", "1.1", "1.2", "2"}.issubset(sent)
    assert draft.markdown.index("window 1.1.") < draft.markdown.index("window 1.2.")
    assert draft.markdown.index("window 1.2.") < draft.markdown.index("window 2.")
    sent.clear()
    restored = await build_document_summary(
        windows, generate=generate, max_concurrency=1, **options
    )
    assert restored == draft and sent == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_summary_progress_reports_real_completed_and_pending_windows(kb_dir):
    from ultrakb.llm_usage_execution import import_usage_execution

    windows = original_windows(kb_dir)
    events = []

    async def generate(messages, stage, originals, sections):
        return {
            "description": "Window",
            "sections": [
                {"id": section, "content": "Accepted original window."} for section in sections
            ],
        }

    with import_usage_execution(kb_dir, "summary", on_event=events.append):
        draft = await build_document_summary(
            windows,
            system_message={"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")},
            generate=generate,
            fits=lambda messages: len(
                json.loads(messages[1]["content"].split("\n", 1)[1])["content"]
            )
            <= 120,
            max_concurrency=2,
        )
    progress = [event["data"] for event in events if event.get("event") == "compile_work"]
    assert progress and progress[0]["pending"] > 0
    assert progress[-1]["completed"] == len(draft.ranges)
    assert progress[-1]["pending"] == progress[-1]["in_flight"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_parallel_summary_obeys_shared_slots_and_admits_the_plan_budget(kb_dir):
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelBudgetExceeded,
        ModelCallPolicy,
        RoleBindings,
    )

    windows = original_windows(kb_dir)
    options = dict(
        system_message={"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")},
        fits=lambda messages: len(json.loads(messages[1]["content"].split("\n", 1)[1])["content"])
        <= 240,
        max_concurrency=2,
    )
    bindings = RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle())

    async def forbidden(*args):
        raise AssertionError("No summary may send without retaining the required plan allowance")

    limited = CompletionExecutor(bindings, ModelCallPolicy(max_calls=2, concurrency=2))
    with limited.activate(), pytest.raises(ModelBudgetExceeded):
        await build_document_summary(windows, generate=forbidden, **options)
    assert limited.calls == 0
    active = peak = 0

    async def generate(messages, stage, originals, sections):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {
            "description": "Window",
            "sections": [
                {"id": section, "content": "Accepted original window."} for section in sections
            ],
        }

    serial = CompletionExecutor(bindings, ModelCallPolicy(concurrency=1))
    with serial.activate():
        await build_document_summary(windows, generate=generate, **options)
    assert peak == 1
