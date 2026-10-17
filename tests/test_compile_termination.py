"""Termination at the real compilation factory and application publication seams."""

import asyncio

import litellm
import pytest

from ultrakb.llm_execution import ModelBudgetExceeded, ModelDeadlineExceeded
from ultrakb.locks import LockCancelled

pytest_plugins = ("test_pdf_readback",)


def terminal_errors():
    return [
        ModelBudgetExceeded("fixture budget exhausted"),
        ModelDeadlineExceeded("fixture deadline expired"),
        LockCancelled("fixture cancelled"),
        asyncio.CancelledError("fixture async cancellation"),
        litellm.AuthenticationError("fixture credentials rejected", "fixture", "openai"),
    ]


@pytest.mark.parametrize("error", terminal_errors(), ids=lambda error: type(error).__name__)
def test_terminal_factory_failure_never_waits_or_restarts(monkeypatch, error):
    from ultrakb.application.documents import _run_compile_with_retry

    starts, waits = [], []

    async def compile_document():
        starts.append(1)
        raise error

    monkeypatch.setattr("ultrakb.application.documents.time.sleep", waits.append)
    with pytest.raises(type(error)) as raised:
        _run_compile_with_retry(compile_document, "fixture")
    assert raised.value is error
    assert starts == [1]
    assert waits == []


@pytest.mark.parametrize("error", terminal_errors(), ids=lambda error: type(error).__name__)
def test_import_preserves_original_and_head_and_propagates_termination(kb_dir, monkeypatch, error):
    from ultrakb.application.documents import import_document
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.published_knowledge import initialize_publication, read_publication
    from ultrakb.source_catalog import list_sources, read_source_revision
    from ultrakb.unit_publication import list_source_units, read_head, read_unit_publication

    source = kb_dir / "raw/gardening.md"
    source.write_text("# Gardening\nWater seedlings in the morning.\n")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        published = initialize_publication(kb_dir)
    head = read_head(kb_dir)
    starts, waits = [], []

    async def compile_document(*args, **kwargs):
        starts.append(1)
        raise error

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", compile_document)
    monkeypatch.setattr("ultrakb.application.documents.time.sleep", waits.append)
    with pytest.raises(type(error)) as raised:
        import_document(kb_dir, source)
    assert raised.value is error
    assert starts == [1] and waits == []
    assert read_head(kb_dir) == head
    assert read_publication(kb_dir).knowledge_revision_id == published.knowledge_revision_id
    retained = list_sources(kb_dir)[0]
    revision = read_source_revision(kb_dir, retained.target_revision_id)
    assert (kb_dir / revision.original).read_bytes() == source.read_bytes()
    unit = list_source_units(kb_dir, retained.source_id)[0]
    state = read_unit_publication(kb_dir, unit.unit_id)
    assert state.knowledge_revision_id is None
    assert state.stage == "compilation"
    assert state.error_type == type(error).__name__


@pytest.mark.parametrize("recovers", [True, False])
def test_only_transient_failures_restart_with_bounded_attempts(monkeypatch, recovers):
    from ultrakb.application.documents import _run_compile_with_retry

    starts, reports = [], []
    failures = [litellm.APIConnectionError(f"failure {i}", "fixture", "openai") for i in (1, 2)]

    async def compile_document():
        starts.append(1)
        if len(starts) == 1 or not recovers:
            raise failures[len(starts) - 1]

    # Time is the boundary; keep the retry wait deterministic and instantaneous.
    now = [0.0]
    monkeypatch.setattr("ultrakb.application.documents.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "ultrakb.application.documents.time.sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    if recovers:
        _run_compile_with_retry(compile_document, "fixture", report=reports.append)
    else:
        with pytest.raises(litellm.APIConnectionError) as raised:
            _run_compile_with_retry(compile_document, "fixture", report=reports.append)
        assert raised.value.__cause__ is failures[0]
    assert len(starts) == 2
    assert any("APIConnectionError" in message for message in reports)


def test_nontransient_failure_does_not_restart(monkeypatch):
    from ultrakb.application.documents import _run_compile_with_retry
    from ultrakb.llm_execution import StageBudgetExceeded

    for error in (ValueError("Invalid source"), StageBudgetExceeded("Limited retrieval coverage")):
        starts, waits = [], []

        async def compile_document():
            starts.append(1)
            raise error

        monkeypatch.setattr("ultrakb.application.documents.time.sleep", waits.append)
        with pytest.raises(type(error)) as raised:
            _run_compile_with_retry(compile_document, "fixture")
        assert raised.value is error
        assert starts == [1] and waits == []


@pytest.mark.parametrize("stop", ["cancel", "budget", "deadline", "source"])
def test_retry_rechecks_execution_and_source_before_restarting(monkeypatch, stop):
    from ultrakb.application.documents import _run_compile_with_retry
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    executor = CompletionExecutor(
        RoleBindings("openai/gpt-4o-mini", LlmCredentialBundle(api_key="fixture")),
        ModelCallPolicy(max_calls=1),
    )
    starts, waits = [], []
    current = [True]

    def check_current():
        if not current[0]:
            raise ValueError("Source was replaced")

    async def compile_document():
        starts.append(1)
        if stop == "budget":
            executor.reserve()
        if stop == "source":
            current[0] = False
        raise litellm.APIConnectionError("temporary provider failure", "fixture", "openai")

    def wait(seconds):
        waits.append(seconds)
        executor.cancel()

    monkeypatch.setattr("ultrakb.application.documents.time.sleep", wait)
    expected = (
        ValueError
        if stop == "source"
        else ModelBudgetExceeded
        if stop == "budget"
        else LockCancelled
    )
    if stop == "deadline":
        from dataclasses import replace

        executor.policy = replace(executor.policy, deadline=2.0)
        monkeypatch.setattr(
            "ultrakb.application.documents.time.monotonic", lambda: 1.0 if not waits else 3.0
        )

        def wait(seconds):
            waits.append(seconds)

        monkeypatch.setattr("ultrakb.application.documents.time.sleep", wait)
        expected = ModelDeadlineExceeded
    with executor.activate(), pytest.raises(expected):
        _run_compile_with_retry(compile_document, "fixture", check_current=check_current)
    assert starts == [1]
    assert len(waits) == (0 if stop in {"source", "budget"} else 1)


@pytest.mark.parametrize("error", terminal_errors(), ids=lambda error: type(error).__name__)
def test_reprocessing_preserves_last_publication_on_termination(
    kb_dir, pdf_model, monkeypatch, error
):
    from ultrakb.application.documents import import_document
    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.documents import read_document_source
    from ultrakb.unit_publication import read_head

    source = kb_dir / "raw/cooking.md"
    source.write_text("# Cooking\nSimmer for ten minutes.\n")
    first = import_document(kb_dir, source)
    assert first.status == "added", first
    head = read_head(kb_dir)
    before = read_document_source(kb_dir, first.source_id)
    preview = preview_reprocessing(kb_dir, first.source_id)
    starts = []

    async def compile_document(*args, **kwargs):
        starts.append(1)
        raise error

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", compile_document)
    with pytest.raises(type(error)):
        reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert starts == [1]
    after = read_head(kb_dir)
    assert after.knowledge_revision_id == head.knowledge_revision_id
    assert after.inputs == head.inputs
    assert after.generated_baselines == head.generated_baselines
    assert read_document_source(kb_dir, first.source_id)["content"] == before["content"]
