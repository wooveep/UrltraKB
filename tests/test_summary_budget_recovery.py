"""Nested summary delivery failures retain exact source coverage across restart."""

import json
from types import SimpleNamespace

import pytest

from ultrakb.agent.document_summary import build_document_summary
from ultrakb.agent.page_response import PageResponseError
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.compile_checkpoints import CompileCheckpoints, digest
from ultrakb.locks import LockCancelled, atomic_write_text

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_nested_failed_batches_resume_only_missing_children(kb_dir):
    original = "\n\n".join(f"Original condition {i}: keep the inlet closed." for i in range(8))
    atomic_write_text(kb_dir / "wiki/sources/manual.md", original)
    reader = CompileSourceWindows(kb_dir / "wiki", "manual")
    system = {"role": "system", "content": "Preserve original conditions."}
    task = SimpleNamespace(task_id="a" * 32, parent_task_id=None)
    checkpoints = CompileCheckpoints(kb_dir, task, {"document": "manual"}, {})
    checkpoints.prepare_summary_delivery()
    seen = []

    async def generate(messages, stage, sources, sections):
        key = stage.removeprefix("document-summary:")
        seen.append(key)
        if key in {"1", "1.1"}:
            code = "summary_missing_sections" if key == "1" else "summary_invalid_sections"
            checkpoints.record_failure("summary:" + key, digest(messages), code)
            raise PageResponseError(code, "Injected validated delivery failure")
        if key == "1.1.2":
            raise LockCancelled("Stop after the first accepted grandchild")
        return answer(sources[0], sections)

    def answer(source, sections):
        return {
            "description": "Original conditions.",
            "sections": [{"id": name, "content": source["content"]} for name in sections],
        }

    with pytest.raises(LockCancelled):
        await build_document_summary(
            reader,
            system_message=system,
            generate=generate,
            fits=lambda _: True,
            checkpoints=checkpoints,
        )
    assert seen == ["1", "1.1", "1.1.1", "1.1.2"]
    restored = CompileCheckpoints(kb_dir, task, {"document": "manual"}, {})
    remaining, supplied = [], []

    async def resume(messages, stage, sources, sections):
        remaining.append(stage.removeprefix("document-summary:"))
        supplied.append(sources[0])
        return answer(sources[0], sections)

    draft = await build_document_summary(
        reader, system_message=system, generate=resume, fits=lambda _: True, checkpoints=restored
    )
    assert remaining == ["1.1.2", "1.2"]
    spans = [span for batch in draft.ranges for span in batch["source_spans"]]
    assert spans[0][0] == 0 and spans[-1][1] == len(original)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    assert all(f"Original condition {i}" in draft.markdown for i in range(8))
    assert all(
        context["primary"] is False
        for source in supplied
        for context in source["supplementary_context"]
    )

    async def forbidden(*args):
        raise AssertionError("Accepted batches must survive another restart")

    assert (
        await build_document_summary(
            reader,
            system_message=system,
            generate=forbidden,
            fits=lambda _: True,
            checkpoints=restored,
        )
        == draft
    )
    manifest = json.loads(restored.path.read_text())
    assert manifest["entries"]["summary-split:1.1"]["state"] == "accepted"


@pytest.mark.asyncio
async def test_received_repair_survives_restart_before_validation(kb_dir):
    from ultrakb.agent.document_summary import summary_messages, window_sections

    original = "Keep the inlet closed until inspection is complete."
    atomic_write_text(kb_dir / "wiki/sources/manual.md", original)
    reader = CompileSourceWindows(kb_dir / "wiki", "manual")
    task = SimpleNamespace(task_id="b" * 32, parent_task_id=None, compiler_request_version=2)
    checkpoints = CompileCheckpoints(kb_dir, task, {"document": "manual"}, {})
    checkpoints.prepare_summary_delivery()
    system = {"role": "system", "content": "Retain the source conditions."}
    window = next(reader.iter_document_windows(fits=lambda _: True))
    messages = summary_messages(system, "manual", window, structured=True)
    sections = tuple(s["id"] for s in window_sections(window))
    signature = digest(messages)
    checkpoints.claim_repair("summary:1", signature)
    raw = json.dumps(
        {
            "description": "Inspection condition",
            "sections": [
                {
                    "id": name,
                    "parts": [{"kind": "prose", "text": original, "refs": []}],
                }
                for name in sections
            ],
        }
    )
    retained = checkpoints.record_received_response(
        "summary:1",
        signature,
        raw,
        stage="document-summary:1",
        response_protocol="summary-parts-json",
        expected_sections=sections,
        code="response_received",
        attempt=2,
    )
    restored = CompileCheckpoints(kb_dir, task, {"document": "manual"}, {})

    async def forbidden(*args):
        raise AssertionError("The received repair must be validated without another send")

    draft = await build_document_summary(
        reader, system_message=system, generate=forbidden, fits=lambda _: True, checkpoints=restored
    )
    assert original in draft.markdown
    assert (kb_dir / retained).read_text() == raw
    entry = json.loads(restored.path.read_text())["entries"]["summary:1"]
    assert entry["state"] == "accepted" and entry["repair_used"] is True
