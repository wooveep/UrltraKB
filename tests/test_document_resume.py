"""Resume completed evidence and drafts without weakening publication checks."""

import json
from collections import Counter

import litellm
import pytest
import yaml

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from openkb.config import DEFAULT_CONFIG
from openkb.processing import ExecutionBudget, ProcessingIncomplete, RequestLimits
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.fixture(autouse=True)
def offline_settings(kb_dir):
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config.update(model="openai/offline-test", language="en", navigation={"enabled": False})
    config["processing"] = {**DEFAULT_CONFIG["processing"], "concurrency": 1}
    path.write_text(yaml.safe_dump(config))


def document(tmp_path, text="Alpha requirement.\n\nBeta requirement."):
    source = tmp_path / "audit.md"
    source.write_text(text)
    return source


@pytest.mark.parametrize(
    "stage,defect",
    [
        ("planning", "json"),
        ("planning", "coverage"),
        ("generation", "json"),
        ("generation", "coverage"),
        ("verification", "json"),
        ("verification", "uncertain"),
    ],
)
def test_one_bad_completed_response_can_recover(kb_dir, tmp_path, monkeypatch, stage, defect):
    calls = Counter()

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        current = payload["stage"]
        calls[current] += 1
        value = evidence_response(payload)
        bad = current == stage and calls[current] == 1
        if bad and defect == "coverage":
            if stage == "planning":
                value["topics"][0]["members"] = []
            else:
                value["covered"] = []
        if bad and defect == "uncertain":
            value = {"verdict": "uncertain", "reason": "Temporary ambiguity in this review."}
        output = response(value)
        if bad and defect == "json":
            output.choices[0].message.content = "{invalid"
        return output

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, document(tmp_path))
    assert result.knowledge_compilation == "completed"


def test_resume_reuses_successful_split_children_without_parent_call(kb_dir, tmp_path, monkeypatch):
    phase = 1
    calls = {1: [], 2: []}

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            calls[phase].append(len(payload["units"]))
            if len(payload["units"]) > 1:
                value["units"].pop()
        if payload["stage"] == "planning" and phase == 1:
            value = {"topics": []}
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    first = import_document(kb_dir, document(tmp_path))
    assert first.stage == "planning"
    phase = 2
    second = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert second.knowledge_compilation == "completed", second
    assert calls[2] == []


def test_resume_verification_does_not_regenerate_received_draft(kb_dir, tmp_path, monkeypatch):
    phase = 1
    calls = Counter()

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls[(phase, payload["stage"])] += 1
        value = evidence_response(payload)
        if phase == 1 and payload["stage"] == "verification":
            value = {}
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    first = import_document(kb_dir, document(tmp_path))
    assert first.reason == "evidence_verification_invalid"
    phase = 2
    second = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert second.knowledge_compilation == "completed", second
    assert calls[(2, "generation")] == 0


@pytest.mark.parametrize(
    "suffix", ["\n\n![Missing](missing.png)", "\n\n```text\nAn unfinished code fence."]
)
def test_markdown_usable_body_can_compile_with_omission_notice(
    kb_dir, tmp_path, monkeypatch, suffix
):
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **k: response(evidence_response(json.loads(k["messages"][-1]["content"]))),
    )
    result = import_document(kb_dir, document(tmp_path, "A complete requirement." + suffix))
    assert result.knowledge_compilation == "completed"


def test_persistent_local_fact_defect_keeps_publication_pending(kb_dir, tmp_path, monkeypatch):
    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            bad = {u["id"] for u in payload["units"] if "Beta" in u["text"]}
            value["units"] = [u for u in value["units"] if u["id"] not in bad]
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, document(tmp_path))
    pages = list((kb_dir / "wiki/concepts").glob("*.md"))
    assert result.reason == "section_coverage_incomplete"
    assert not pages


def test_explicit_request_count_stops_even_with_unlimited_tokens():
    limits = RequestLimits.from_config(
        {**DEFAULT_CONFIG, "processing": {**DEFAULT_CONFIG["processing"], "max_requests": 200}}
    )
    budget = ExecutionBudget(limits)
    budget.attempts = limits.max_requests
    with pytest.raises(ProcessingIncomplete) as failure:
        budget.reserve({"model": "openai/offline-test", "messages": []})
    assert failure.value.reason == "request_budget_exhausted"


def test_semantic_unsupported_still_prevents_publication(kb_dir, tmp_path, monkeypatch):
    calls = Counter()

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls[payload["stage"]] += 1
        value = evidence_response(payload)
        if payload["stage"] == "verification":
            value = {"verdict": "unsupported", "reason": "Candidate changes a literal condition."}
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, document(tmp_path))
    assert result.reason == "knowledge_evidence_mismatch"
    assert calls["generation"] == calls["verification"] == 2
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_empty_extraction_of_factual_body_is_not_success(kb_dir, tmp_path, monkeypatch):
    calls = Counter()

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls[payload["stage"]] += 1
        assert payload["stage"] == "facts"
        return response(
            {
                "units": [
                    {"id": u["id"], "facts": [], "empty_reason": "No useful facts."}
                    for u in payload["units"]
                ]
            }
        )

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(
        kb_dir,
        document(tmp_path, "The required pressure is 37 kPa. Never retry authentication failure."),
    )
    pages = list((kb_dir / "wiki/concepts").glob("*.md"))
    assert result.knowledge_compilation != "completed"
    assert not pages


def test_known_text_encoding_is_detected(kb_dir, tmp_path, monkeypatch):
    source = tmp_path / "utf16.txt"
    source.write_text("Required pressure: 37 kPa.", encoding="utf-16")
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **k: response(evidence_response(json.loads(k["messages"][-1]["content"]))),
    )
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed"


def test_later_pdf_page_failure_can_retain_earlier_pages(kb_dir, tmp_path, monkeypatch):
    import pymupdf

    source = tmp_path / "local-page-error.pdf"
    pdf = pymupdf.open()
    for text in ["Complete first page: pressure 37 kPa.", "Damaged second page."]:
        pdf.new_page().insert_text((50, 50), text)
    pdf.save(source)
    pdf.close()
    original = pymupdf.Page.get_text

    def damaged(self, *args, **kwargs):
        if self.number == 1:
            raise RuntimeError("Synthetic document-local page decode failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_text", damaged)
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **k: response(evidence_response(json.loads(k["messages"][-1]["content"]))),
    )
    result = import_document(kb_dir, source)
    assert result.parse_id is not None


def test_planning_is_bounded_and_resume_skips_completed_plans(kb_dir, tmp_path, monkeypatch):
    from openkb.agent.evidence_checkpoints import CompilationCheckpoints
    from openkb.agent.evidence_plan import MAX_PLAN_TOPICS, plan_topics
    from openkb.config import resolve_effective_config
    from openkb.evidence import ParseStore
    from openkb.inputs import prepared_input
    from openkb.parsing import parse_document
    from openkb.progress import progress_reporting
    from openkb.sources import SourceStore

    path = document(tmp_path)
    with prepared_input(path) as ready:
        source = SourceStore(kb_dir).intake(ready)
    parsed = parse_document(kb_dir, source)
    assert ParseStore(kb_dir).compilable(source, parsed)
    settings, _ = resolve_effective_config(kb_dir)
    cp = CompilationCheckpoints(kb_dir, source, parsed, settings, None)
    topics = [f"topic-{i:04d}" for i in range(300)]
    calls = []
    stopped = False
    import openkb.agent.compiler as compiler

    def model(model, messages, *args, **kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload["topics"])
        if not stopped and len(calls) == 2:
            raise ProcessingIncomplete("request_timeout", "planning")
        return json.dumps(evidence_response(payload))

    monkeypatch.setattr(compiler, "_llm_call", model)
    with pytest.raises(ProcessingIncomplete):
        plan_topics(
            topics,
            kb_dir,
            settings,
            RequestLimits.from_config(settings),
            cp,
            bundle=None,
            on_event=lambda event: None,
        )
    first = calls[0]
    stopped = True
    calls.clear()
    events = []
    with progress_reporting(events.append):
        groups = plan_topics(
            topics,
            kb_dir,
            settings,
            RequestLimits.from_config(settings),
            cp,
            bundle=None,
            on_event=lambda event: None,
        )
    assert sorted(member for g in groups for member in g["members"]) == topics
    assert all(len(batch) <= MAX_PLAN_TOPICS for batch in calls)
    assert not set(first) & {topic for batch in calls for topic in batch}
    assert any(
        step["phase"] == "planning" and step["completed"] == 300
        for event in events
        for step in event["progress"]
    )


def test_pdf_storage_error_is_not_treated_as_a_page_omission(kb_dir, tmp_path, monkeypatch):
    import pymupdf

    from openkb.parsing_pdf import parse_pdf
    from openkb.sources import SourceStore

    path = tmp_path / "storage.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().draw_circle((50, 50), 20)
        pdf.save(path)

    def fail(*args):
        raise OSError("Synthetic full disk")

    monkeypatch.setattr(SourceStore, "put_bytes", fail)
    with pytest.raises(OSError, match="full disk"):
        parse_pdf(path, SourceStore(kb_dir))


def test_fact_resume_reuses_old_batches_after_batch_size_change(kb_dir, tmp_path, monkeypatch):
    source = document(tmp_path, "\n\n".join(f"Requirement {i}." for i in range(16)))
    calls = []
    stop = True

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls.append(payload["stage"])
        if stop and payload["stage"] == "planning":
            raise ProcessingIncomplete("request_timeout", "planning")
        return response(evidence_response(payload))

    monkeypatch.setattr(litellm, "completion", completion)
    first = import_document(kb_dir, source)
    assert first.reason == "request_timeout"
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["processing"].update(context_tokens=4096, output_tokens=1024)
    path.write_text(yaml.safe_dump(config))
    calls.clear()
    stop = False
    result = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert result.knowledge_compilation == "completed", result
    assert "facts" not in calls


def test_parallel_pages_finish_independent_work_and_resume_only_failed_topic(
    kb_dir, tmp_path, monkeypatch
):
    import threading

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"]["concurrency"] = 4
    config_path.write_text(yaml.safe_dump(config))
    source = document(tmp_path, "\n\n".join(f"Requirement {i}." for i in range(6)))
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    first = True
    calls = []
    starts = 0

    def completion(**kwargs):
        nonlocal starts
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            for row, unit in zip(value["units"], payload["units"]):
                row["facts"][0]["topic"] = "topic-" + unit["text"].split()[1].rstrip(".")
        elif payload["stage"] == "planning":
            value = {
                "topics": [
                    {"name": topic, "title": topic, "kind": "concept", "members": [topic]}
                    for topic in payload["topics"]
                ]
            }
        elif payload["stage"] == "generation":
            with lock:
                starts += 1
                number = starts
                calls.append(payload["title"])
            if first and number <= 4:
                barrier.wait(timeout=3)
            if first and payload["title"] == "topic-0":
                value["covered"] = []
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.reason == "topic_generation_incomplete", result
    assert set(calls) == {f"topic-{i}" for i in range(6)}
    first = False
    calls.clear()
    result = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert result.knowledge_compilation == "completed", result
    assert calls == ["topic-0"]
    assert len(list((kb_dir / "wiki/concepts").glob("*.md"))) == 6


@pytest.mark.parametrize("stop_at", ["verification", "generated"])
@pytest.mark.parametrize("format", ["text", "docx_with_attachment"])
def test_user_stop_resumes_without_parsing_or_repeating_completed_model_work(
    kb_dir, tmp_path, monkeypatch, stop_at, format
):
    import threading

    import openkb.parsing as parsing
    from openkb.cancellation import cancellation_scope

    stopped = threading.Event()
    calls = Counter()

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls[payload["stage"]] += 1
        return response(evidence_response(payload))

    def progress(event):
        if event.get("operation") == stop_at or event.get("stage") == stop_at:
            stopped.set()

    monkeypatch.setattr(litellm, "completion", completion)
    source = document(tmp_path)
    if format == "docx_with_attachment":
        from tests.document_fixtures import write_docx
        from tests.docx_attachment_fixtures import attached_docx

        child = tmp_path / "child.docx"
        write_docx(child, "<w:p><w:r><w:t>Required pressure: 37 kPa.</w:t></w:r></w:p>")
        source = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    with cancellation_scope(stopped.is_set):
        first = import_document(kb_dir, source, on_event=progress)
    assert first.knowledge_compilation == "stopped", first
    assert first.parse_id is not None

    def forbidden(*args, **kwargs):
        pytest.fail("Resume reparsed an unchanged source")

    monkeypatch.setattr(parsing, "parse_text", forbidden)
    import openkb.parsing_docx as parsing_docx

    monkeypatch.setattr(parsing_docx, "parse_docx", forbidden)
    calls.clear()
    result = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert result.knowledge_compilation == "completed", result
    assert result.parse_id == first.parse_id
    assert calls["facts"] == calls["planning"] == calls["generation"] == 0
    assert calls["verification"] == (1 if stop_at == "verification" else 0)


def test_failed_fact_does_not_discard_or_prevent_later_valid_units(kb_dir, tmp_path, monkeypatch):
    first = True
    resumed = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            if first:
                bad = {u["id"] for u in payload["units"] if "Alpha" in u["text"]}
                value["units"] = [u for u in value["units"] if u["id"] not in bad]
            else:
                resumed.extend(unit["text"] for unit in payload["units"])
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(
        kb_dir, document(tmp_path, "Alpha requirement.\n\nBeta requirement.\n\nGamma requirement.")
    )
    assert result.reason == "section_coverage_incomplete", result
    first = False
    result = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert result.knowledge_compilation == "completed", result
    assert resumed == ["Alpha requirement."]
