"""Resume accepted compile results within the original durable task allowance."""

import json

import pytest


def accepted_summary_input(window):
    from ultrakb.agent.document_summary import window_sections

    return {
        "description": "Accepted operating condition",
        "sections": [
            {"id": section["id"], "content": window["content"][section["start"] : section["end"]]}
            for section in window_sections(window)
        ],
    }


@pytest.mark.asyncio
async def test_four_summary_batches_resume_after_three_with_one_bounded_repair(
    kb_dir, monkeypatch, live_llm
):
    from test_document_summary import generate_summary

    from ultrakb.agent.compiler import _write_summary
    from ultrakb.agent.document_summary import (
        SYSTEM_TEMPLATE,
        build_document_summary,
        summary_messages,
    )
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.compile_checkpoints import CompileCheckpoints, digest
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.locks import LockCancelled, atomic_write_text
    from ultrakb.model_task import TaskJournal, read_task

    original = (
        "Keep the inlet closed.\n\nInspect before opening.\n\n"
        "Check the interlock.\n\nTrained technicians only."
    )
    atomic_write_text(kb_dir / "wiki/sources/manual.md", original)
    summary = kb_dir / "wiki/summaries/manual.md"
    atomic_write_text(
        summary, "---\nfull_text: sources/manual.md\n---\nPrevious successful result."
    )
    before = summary.read_bytes()
    reader = CompileSourceWindows(kb_dir / "wiki", "manual")
    batches = list(reader.iter_document_windows(fits=lambda window: len(window["content"]) <= 26))
    assert len(batches) == 4
    system = {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")}
    live_llm.install(monkeypatch)
    stopped = False
    bindings = RoleBindings("deepseek/deepseek-flash", live_llm.credentials)
    # Three accepted sends, one new summary, its bounded repair and one plan.
    policy = ModelCallPolicy(max_calls=6, retries=0, cancelled=lambda: stopped)
    first = CompletionExecutor(bindings, policy)
    first.budget.restore(3, {"compile": 3})
    journal = TaskJournal(kb_dir, first)
    checkpoints = CompileCheckpoints(
        kb_dir, journal.record, {"document": "manual"}, {"protocol": "summary-json"}
    )
    for index, batch in enumerate(batches[:3], 1):
        request = summary_messages(system, "manual", batch)
        checkpoints.save(f"summary:{index}", digest(request), accepted_summary_input(batch))
    checkpoint_bytes = {
        path: path.read_bytes() for path in checkpoints.directory.glob("payload-*.json")
    }
    stopped = True

    async def forbidden(*args):
        raise AssertionError("Interrupted compilation must not generate a response")

    def fits(messages):
        return len(json.loads(messages[1]["content"].split("\n", 1)[1])["content"]) <= 26

    with first.activate(), pytest.raises(LockCancelled):
        await build_document_summary(
            reader, system_message=system, generate=forbidden, fits=fits, checkpoints=checkpoints
        )
    assert first.calls == 3 and summary.read_bytes() == before
    stopped = False
    resumed = CompletionExecutor(bindings, policy)
    TaskJournal(kb_dir, resumed, resume=journal.record.task_id)
    restored = CompileCheckpoints(
        kb_dir, journal.record, {"document": "manual"}, checkpoints.conditions
    )
    sent = []

    async def generate(request, stage, originals, sections):
        sent.append(request)
        return await generate_summary(request, stage, originals, sections, live_llm)

    with resumed.activate():
        draft = await build_document_summary(
            reader, system_message=system, generate=generate, fits=fits, checkpoints=restored
        )
        resumed.budget.require("compile", 1)  # The remaining planning step retains its allowance.
    assert len(sent) == 1
    assert resumed.calls == read_task(kb_dir, journal.record.task_id).used
    assert 1 <= resumed.calls - 3 <= 2
    assert all(path.read_bytes() == saved for path, saved in checkpoint_bytes.items())
    assert (
        json.loads(sent[0][1]["content"].split("\n", 1)[1])["selection_offset"]
        == batches[3]["selection_offset"]
    )
    assert "".join(batch["content"] for batch in batches) == original
    _write_summary(
        kb_dir / "wiki",
        "manual",
        draft.markdown,
        doc_type="pageindex",
        description=draft.description,
        full_text="sources/manual.md",
    )
    saved = summary.read_text().lower()
    assert "trained" in saved and "technicians" in saved
    assert (
        "closed" in saved and "interlock" in saved and "inspection" not in before.decode().lower()
    )


@pytest.mark.parametrize("used,admitted", [(3, True), (4, False)])
def test_one_missing_summary_and_plan_require_two_remaining_sends(used, admitted):
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.execution_budget import admit_document_summary
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelBudgetExceeded,
        ModelCallPolicy,
        RoleBindings,
    )

    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle()),
        ModelCallPolicy(max_calls=5, retries=0),
    )
    executor.budget.restore(used, {"compile": used})
    with executor.activate():
        if admitted:
            admit_document_summary(1)
            executor.budget.require("compile", 2)
        else:
            with pytest.raises(ModelBudgetExceeded, match="compile needs at least 2 sends"):
                admit_document_summary(1)
    assert executor.calls == used and executor.budget.snapshot()["used"] == used


@pytest.mark.parametrize("boundary", ["payload", "manifest"])
def test_incomplete_checkpoint_write_is_not_accepted(tmp_path, monkeypatch, boundary):
    from types import SimpleNamespace

    from ultrakb.compile_checkpoints import CompileCheckpoints, digest
    from ultrakb.locks import atomic_write_json

    task = SimpleNamespace(task_id="a" * 32, parent_task_id=None)
    checkpoint = CompileCheckpoints(tmp_path, task, {"document": "manual"}, {"protocol": "v1"})

    def fail(path, value):
        if (boundary == "manifest") == (path.name == "manifest.json"):
            raise OSError("Injected storage failure")
        atomic_write_json(path, value)

    with monkeypatch.context() as patch:
        patch.setattr("ultrakb.compile_checkpoints.atomic_write_json", fail)
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

    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import CompileCheckpoints

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

    from figure_fixtures import record_markdown
    from PIL import Image

    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.compile_checkpoints import CompileCheckpoints, document_checkpoints
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.unit_publication import prepare_compile_view

    images = kb_dir / "wiki/sources/images"
    images.mkdir(exist_ok=True)
    Image.new("RGB", (20, 10), "red").save(images / "figure.png")
    record_markdown(
        kb_dir / "wiki", "sources/manual.md", "# Install\n![Install](images/figure.png)\n"
    )
    relative = "concepts/device.md"
    page = kb_dir / "wiki" / relative
    page.write_text("---\nsources: [sources/manual.md]\n---\nKeep this rule. See `figure.png`.\n")
    original = page.read_bytes()
    old_digest = hashlib.sha256(original).hexdigest()
    if retained_projection:
        with kb_ingest_lock(kb_dir / ".ultrakb"), prepare_compile_view(kb_dir) as view:
            old_digest = hashlib.sha256((view.scope.wiki_dir / relative).read_bytes()).hexdigest()

    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
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


def test_application_retry_restores_plan_and_summary_with_one_send_left(kb_dir):
    from ultrakb.agent.compile_delivery import publish_compile_results
    from ultrakb.agent.compile_plan import normalize_plan
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.compile_checkpoints import cleanup_published_checkpoints, document_checkpoints
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.locks import LockCancelled, atomic_write_text, kb_ingest_lock
    from ultrakb.model_task import read_task

    source = kb_dir / "wiki/sources/manual.md"
    original = "Winter operation requires supervision."
    atomic_write_text(source, original)
    bindings = RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle())
    stopped = False
    policy = ModelCallPolicy(
        max_calls=3, retries=0, compile_repair_calls=0, cancelled=lambda: stopped
    )
    first = CompletionExecutor(bindings, policy)
    first.budget.restore(2, {"compile": 2})
    context = ExecutionContext(executor=first)
    accepted_summary = json.dumps(
        {"description": "Winter requirement", "content": "# Winter\n\nSupervision required."}
    )
    accepted_plan = {"concepts": {"create": [{"name": "winter-operation"}]}}
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        checkpoints = document_checkpoints(kb_dir, "legacy", "manual")
        checkpoints.save("short-summary", "summary-request", accepted_summary)
        checkpoints.save("plan", "plan-request", accepted_plan)
        stopped = True
        with pytest.raises(LockCancelled):
            checkpoints.load("plan", "plan-request")
    assert first.calls == 2
    assert not list((kb_dir / "wiki/summaries").glob("*.md"))
    stopped = False
    resumed = CompletionExecutor(bindings, policy)
    next_context = ExecutionContext(
        executor=resumed, resume_model_task_id=context.model_task.record.task_id
    )
    with kb_ingest_lock(kb_dir / ".ultrakb"), next_context.begin(kb_dir):
        checkpoints = document_checkpoints(kb_dir, "legacy", "manual")
        summary = json.loads(
            validate_page_delivery(
                checkpoints.load("short-summary", "summary-request"),
                response_protocol="summary-page-json",
            )
        )
        plan = normalize_plan(
            checkpoints.load("plan", "plan-request"),
            existing={"concepts": set(), "entities": set()},
            entity_types=frozenset(),
            sanitize=lambda name: name,
        )
        resumed.budget.require("compile", 1)
        # Publication takes explicitly validated candidate data. Real generation
        # of this same protocol is exercised by the live compiler regressions.
        received = json.loads(
            validate_page_delivery(
                json.dumps(
                    {
                        "description": "Winter requirement",
                        "content": "# Winter operation\n\nSupervision required.",
                    }
                ),
            )
        )
        publish_compile_results(
            kb_dir / "wiki",
            "manual",
            plan,
            summary=summary["content"],
            concept_results=[
                ("winter-operation", received["content"], False, received["description"])
            ],
            doc_brief=summary["description"],
            rewrite_summary=True,
        )
        cleanup_published_checkpoints(kb_dir, "legacy", "manual")
    assert resumed.calls == read_task(kb_dir, context.model_task.record.task_id).used == 2
    assert len(list((kb_dir / ".ultrakb/model-tasks").glob("*.json"))) == 1
    assert "Supervision required." in (kb_dir / "wiki/concepts/winter-operation.md").read_text()
    assert not list((kb_dir / ".ultrakb/compile-checkpoints").rglob("manifest.json"))
    assert source.read_text() == original


def test_cleanup_failure_after_publication_cannot_reapply_the_result(kb_dir, monkeypatch):
    import shutil

    from publication_fixtures import publish_document

    import ultrakb.unit_publication as publication
    from ultrakb.application.source_retry import retry_source
    from ultrakb.compile_checkpoints import document_checkpoints
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.locks import atomic_write_text
    from ultrakb.unit_publication import read_head

    source = kb_dir / "manual.txt"
    atomic_write_text(source, "Winter requires supervision.")
    actual_publish, actual_remove = publication.publish_unit_revision, shutil.rmtree

    def publish_with_checkpoint(root, admission, unit, revision, state, view, **options):
        checkpoints = document_checkpoints(root, state.view_id, unit.doc_name)
        checkpoints.save("short-summary", "accepted-input", {"content": "Supervision required."})
        return actual_publish(root, admission, unit, revision, state, view, **options)

    def fail_cleanup(path, *args, **kwargs):
        if "compile-checkpoints" in str(path):
            raise OSError("Injected post-publication cleanup failure")
        return actual_remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "publish_unit_revision", publish_with_checkpoint)
        patch.setattr(shutil, "rmtree", fail_cleanup)
        result = publish_document(
            kb_dir, source, "Winter requires supervision.", bundle=LlmCredentialBundle()
        )
    assert result.status == "added", result
    head = read_head(kb_dir)
    assert list((kb_dir / ".ultrakb/compile-checkpoints").rglob("manifest.json"))
    replay = retry_source(kb_dir, result.source_id)
    assert replay.status in {"added", "skipped"}, replay
    assert read_head(kb_dir) == head
    assert replay.model_usage["current"]["actual_sends"] == 0
