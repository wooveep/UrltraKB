"""Resume accepted compile results within the original durable task allowance."""

import json

import pytest
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


def summary_reply(request):
    batch = json.loads(request["messages"][1]["content"].split("\n", 1)[1])
    return response(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "description": "Retained conditions",
                    "sections": [
                        {"id": s["id"], "content": "Accepted operating condition."}
                        for s in batch["output_sections"]
                    ],
                }
            ),
        }
    )


@pytest.mark.asyncio
async def test_four_summary_batches_resume_after_three_with_only_two_sends_left(
    kb_dir, model_service
):
    from openkb.agent.compiler import compile_long_doc
    from openkb.application.execution import ExecutionContext
    from openkb.locks import LockCancelled, kb_ingest_lock

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  max_calls: 5\n  retries: 0\n"
    )
    (kb_dir / "wiki/sources/manual.md").write_text(("A" * 998 + "\n\n") * 80)
    summary = kb_dir / "wiki/summaries/manual.md"
    summary.write_text("---\nfull_text: sources/manual.md\n---\nPrevious successful result.")
    before = summary.read_bytes()
    checkpoint_root = kb_dir / ".openkb/compile-checkpoints"

    def interrupted():
        return (
            sum(
                entry["state"] == "accepted"
                for path in checkpoint_root.rglob("manifest.json")
                for entry in json.loads(path.read_text())["entries"].values()
            )
            >= 3
        )

    first = ExecutionContext(cancelled=interrupted)
    model_service.replies.extend(
        [(200, summary_reply)] * 4 + [(200, response({"role": "assistant", "content": "{}"}))]
    )
    with kb_ingest_lock(kb_dir / ".openkb"), first.begin(kb_dir):
        with pytest.raises(LockCancelled):
            await compile_long_doc("manual", summary, "id", kb_dir, "ignored", navigation=[])
    assert len(model_service.requests) == first.executor.calls == 3
    assert summary.read_bytes() == before
    resumed = ExecutionContext(resume_model_task_id=first.model_task.record.task_id)
    with kb_ingest_lock(kb_dir / ".openkb"), resumed.begin(kb_dir):
        await compile_long_doc("manual", summary, "id", kb_dir, "ignored", navigation=[])
    assert len(model_service.requests) == resumed.executor.calls == 5
    assert "Accepted operating condition." in summary.read_text()
    offsets = [
        json.loads(r["messages"][1]["content"].split("\n", 1)[1])["selection_offset"]
        for r in model_service.requests[:4]
    ]
    assert offsets == sorted(set(offsets))


@pytest.mark.parametrize("boundary", ["payload", "manifest"])
def test_incomplete_checkpoint_write_is_not_accepted(tmp_path, monkeypatch, boundary):
    from types import SimpleNamespace

    from openkb.compile_checkpoints import CompileCheckpoints, digest
    from openkb.locks import atomic_write_json

    task = SimpleNamespace(task_id="a" * 32, parent_task_id=None)
    checkpoint = CompileCheckpoints(tmp_path, task, {"document": "manual"}, {"protocol": "v1"})

    def fail(path, value):
        if (boundary == "manifest") == (path.name == "manifest.json"):
            raise OSError("Injected storage failure")
        atomic_write_json(path, value)

    with monkeypatch.context() as patch:
        patch.setattr("openkb.compile_checkpoints.atomic_write_json", fail)
        with pytest.raises(OSError, match="Injected"):
            checkpoint.save("summary:1", "request", {"content": "Accepted"})
    assert checkpoint.load("summary:1", "request") is None
    checkpoint.save("summary:1", "request", {"content": "Accepted"})
    assert checkpoint.load("summary:1", "request") == {"content": "Accepted"}
    payload = checkpoint.directory / f"payload-{digest('summary:1')}.json"
    payload.write_text('{"key":"summary:1", "result":{"content":"Damaged"}}')
    assert checkpoint.load("summary:1", "request") is None


def test_changed_conditions_and_page_base_cannot_reuse_accepted_payload(tmp_path):
    from types import SimpleNamespace

    from openkb.agent.page_response import PageResponseError
    from openkb.compile_checkpoints import CompileCheckpoints

    task = SimpleNamespace(task_id="a" * 32, parent_task_id=None)
    first = CompileCheckpoints(tmp_path, task, {"document": "manual"}, {"protocol": "v1"})
    first.save("summary:1", "request", {"content": "Accepted"})
    changed = CompileCheckpoints(tmp_path, task, {"document": "manual"}, {"protocol": "v2"})
    assert changed.load("summary:1", "request") is None
    assert first.load("summary:1", "changed-input") is None
    first.bind_page("concepts/device.md", "old-base")
    restarted = CompileCheckpoints(tmp_path, task, {"document": "manual"}, {"protocol": "v1"})
    with pytest.raises(PageResponseError, match="page_update_stale"):
        restarted.bind_page("concepts/device.md", "new-base")
    first.claim_conflict("conflict", "request")
    first.claim_repair("conflict", "request")
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        restarted.require_generation("conflict", "request")
    with pytest.raises(PageResponseError, match="compile_conflict_exhausted"):
        restarted.claim_conflict("conflict", "request")


@pytest.mark.parametrize("human_edit", ["none", "before-prepare", "after-prepare"])
@pytest.mark.parametrize("retained_projection", [False, True])
def test_text_figure_protocol_preserves_old_base_and_only_accepts_figure_projection(
    kb_dir, human_edit, retained_projection
):
    import hashlib

    from PIL import Image

    from openkb.agent.page_response import PageResponseError
    from openkb.application.execution import ExecutionContext
    from openkb.compile_checkpoints import CompileCheckpoints, document_checkpoints
    from openkb.locks import kb_ingest_lock
    from openkb.unit_publication import prepare_compile_view

    images = kb_dir / "wiki/sources/images"
    images.mkdir(exist_ok=True)
    Image.new("RGB", (20, 10), "red").save(images / "figure.png")
    (kb_dir / "wiki/sources/manual.md").write_text("# Install\n![Install](images/figure.png)\n")
    relative = "concepts/device.md"
    page = kb_dir / "wiki" / relative
    page.write_text("---\nsources: [sources/manual.md]\n---\nKeep this rule. See `figure.png`.\n")
    original = page.read_bytes()
    old_digest = hashlib.sha256(original).hexdigest()
    if retained_projection:
        with kb_ingest_lock(kb_dir / ".openkb"), prepare_compile_view(kb_dir) as view:
            old_digest = hashlib.sha256((view.scope.wiki_dir / relative).read_bytes()).hexdigest()

    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir):
        current = document_checkpoints(kb_dir, "legacy", "manual")
        old = CompileCheckpoints(
            kb_dir,
            context.model_task.record,
            current.identity,
            {**current.conditions, "compiler_protocol": "accepted-results-v1"},
        )
        old.bind_page(relative, old_digest)
        old.bind_page("concepts/other.md", "unchanged-other-page")
        old.save("summary:1", "old-request", {"content": "Old draft"})
        used = context.model_task.record.used
        assert current.load("summary:1", "old-request") is None
        # Saving a new-protocol result must not erase the retained base either.
        current.save("summary:1", "new-request", {"content": "New draft"})
        with pytest.raises(PageResponseError, match="page_update_stale"):
            current.bind_page("concepts/other.md", "changed-other-page")
        if human_edit == "before-prepare":
            page.write_bytes(original + b"A human addition.\n")
        with prepare_compile_view(kb_dir) as view:
            projected = view.scope.wiki_dir / relative
            assert b"figure-assets" in projected.read_bytes()
            if human_edit == "after-prepare":
                projected.write_bytes(projected.read_bytes() + b"A human addition.\n")
            projected_digest = hashlib.sha256(projected.read_bytes()).hexdigest()
            if human_edit == "none":
                current.bind_page(relative, projected_digest, page_path=projected)
                current.bind_page(relative, projected_digest, page_path=projected)
            else:
                with pytest.raises(PageResponseError, match="page_update_stale"):
                    current.bind_page(relative, projected_digest, page_path=projected)
        assert context.model_task.record.used == used
        with pytest.raises(PageResponseError, match="page_update_stale"):
            current.bind_page(relative, "later-human-change")
        assert page.read_bytes() == original + (
            b"A human addition.\n" if human_edit == "before-prepare" else b""
        )


def test_application_retry_restores_plan_and_summary_with_one_send_left(kb_dir, model_service):
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.application.source_retry import retry_source
    from openkb.locks import LockCancelled

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n"
        "  retries: 0\n  max_calls: 3\n  compile_repair_calls: 0\n"
    )
    source = kb_dir / "manual.txt"
    source.write_text("Winter operation requires supervision.")
    for reply in [
        {"description": "Winter requirement", "content": "# Winter\n\nSupervision required."},
        {"concepts": {"create": [{"name": "winter-operation"}]}},
        {
            "description": "Winter requirement",
            "content": "# Winter operation\n\nSupervision required.",
        },
    ]:
        model_service.replies.append(
            (200, response({"role": "assistant", "content": json.dumps(reply)}))
        )

    def paused():
        return (
            sum(
                entry.get("state") == "accepted"
                for p in (kb_dir / ".openkb/compile-checkpoints").rglob("manifest.json")
                for entry in json.loads(p.read_text())["entries"].values()
            )
            >= 2
        )

    first = ExecutionContext(cancelled=paused)
    with pytest.raises(LockCancelled):
        import_document(kb_dir, source, context=first)
    assert len(model_service.requests) == first.executor.calls == 2
    assert not list((kb_dir / "wiki/summaries").glob("*.md"))
    resumed = retry_source(kb_dir, next(iter(first.model_task.record.inputs)))
    assert resumed.status == "added", resumed
    assert len(model_service.requests) == 3
    tasks = list((kb_dir / ".openkb/model-tasks").glob("*.json"))
    assert len(tasks) == 1 and json.loads(tasks[0].read_text())["used"] == 3
    assert "Supervision required." in (kb_dir / "wiki/concepts/winter-operation.md").read_text()
    assert not list((kb_dir / ".openkb/compile-checkpoints").rglob("manifest.json"))


def test_cleanup_failure_after_publication_cannot_reapply_the_result(
    kb_dir, model_service, monkeypatch
):
    import shutil

    from openkb.application.documents import import_document
    from openkb.application.source_retry import retry_source
    from openkb.unit_publication import read_head

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text("model: openai/gpt-4o-mini\nlanguage: en\n")
    source = kb_dir / "manual.txt"
    source.write_text("Winter requires supervision.")
    for reply in [{"description": "Winter", "content": "# Winter\n\nSupervision required."}, {}]:
        model_service.replies.append(
            (200, response({"role": "assistant", "content": json.dumps(reply)}))
        )
    remove = shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if "compile-checkpoints" in str(path):
            raise OSError("Injected post-publication cleanup failure")
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("shutil.rmtree", fail_cleanup)
        result = import_document(kb_dir, source)
    assert result.status == "added", result
    head = read_head(kb_dir)
    assert list((kb_dir / ".openkb/compile-checkpoints").rglob("manifest.json"))
    replay = retry_source(kb_dir, result.source_id)
    assert replay.status in {"added", "skipped"}, replay
    assert read_head(kb_dir) == head and len(model_service.requests) == 2
