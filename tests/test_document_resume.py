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
from tests.processing_fixtures import OFFLINE_PROCESSING
from tests.test_adaptive_processing import response


@pytest.fixture(autouse=True)
def offline_settings(kb_dir):
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config.update(model="openai/offline-test", language="en", navigation={"enabled": False})
    # ``DEFAULT_CONFIG`` deliberately has no invented context capacity.  This
    # controlled unknown test model therefore needs the explicit offline
    # contract used by the rest of the document-compilation suite.
    config["processing"] = {
        **OFFLINE_PROCESSING,
        # Keep explicit retry ceilings so tests that deliberately lower the
        # starting request can exercise adaptive continuation.
        "max_context_tokens": 128_000,
        "max_output_tokens": 4_096,
        "concurrency": 1,
    }
    path.write_text(yaml.safe_dump(config))


def document(tmp_path, text="Alpha requirement.\n\nBeta requirement."):
    source = tmp_path / "audit.md"
    source.write_text(text)
    return source


@pytest.mark.parametrize("envelope", ["fence", "bom", "duplicate"])
def test_review_format_recovery_never_discards_a_conflicting_verdict(
    kb_dir, tmp_path, monkeypatch, envelope
):
    calls = Counter()

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stage = payload["stage"]
        calls[stage] += 1
        value = response(evidence_response(payload))
        if stage == "verification":
            raw = value.choices[0].message.content
            value.choices[0].message.content = (
                "```json\n" + raw + "\n```"
                if envelope == "fence"
                else "\ufeff" + raw
                if envelope == "bom"
                else '{"verdict":"unsupported","verdict":"supported",'
                '"reason":"Conflicting conclusions"}'
            )
        return value

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, document(tmp_path))
    if envelope == "duplicate":
        assert result.knowledge_compilation == "completed", result
        assert calls["verification"] == 2
        assert any(row["reason"] == "document_verification_invalid" for row in result.omissions)
        assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    else:
        assert result.knowledge_compilation == "completed", result
        assert calls["verification"] == 1


def test_recovered_split_ranges_cover_the_original_block(kb_dir, tmp_path, monkeypatch):
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(output_tokens=1024, max_output_tokens=1024)
    config_path.write_text(yaml.safe_dump(config))
    text = "\n".join(f"Operation {i} requires its own approval." for i in range(12))
    generation_calls = 0

    def completion(**kwargs):
        nonlocal generation_calls
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = response(evidence_response(payload))
        if payload["stage"] == "generation":
            generation_calls += 1
        if (
            payload["stage"] == "generation"
            and sum(len(item["text"]) for item in payload["evidence"]["blocks"]) > 200
        ):
            value.choices[0].finish_reason = "length"
        return value

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, document(tmp_path, text))
    assert result.knowledge_compilation == "completed", result
    assert result.coverage["status"] == "complete"
    ranges = result.coverage["ranges"]
    assert generation_calls > 1
    assert ranges[0]["start"] == 0 and ranges[-1]["end"] == len(text)
    assert all(left["end"] == right["start"] for left, right in zip(ranges, ranges[1:]))


def test_completed_document_recompiles_when_the_effective_output_cap_is_tightened(
    kb_dir, tmp_path, model_service
):
    source = document(tmp_path)
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(output_tokens=4_096, max_output_tokens=4_096)
    config_path.write_text(yaml.safe_dump(config))

    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    calls = len(model_service)

    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(output_tokens=1_024, max_output_tokens=1_024)
    config_path.write_text(yaml.safe_dump(config))

    repeated = import_document(kb_dir, source)

    assert repeated.status == "added", repeated
    assert repeated.knowledge_compilation == "completed"
    assert len(model_service) > calls
    assert all(call["max_tokens"] <= 1_024 for call in model_service[calls:])


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
                value["page_changes"][0]["subject_ranges"] = []
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
    if defect == "uncertain":
        assert calls["verification"] == 1
        assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    else:
        assert calls[stage] == 2


def test_resume_reuses_accepted_document_plan_without_replanning(kb_dir, tmp_path, monkeypatch):
    phase = 1
    calls = Counter()

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls[(phase, payload["stage"])] += 1
        value = evidence_response(payload)
        if phase == 1 and payload["stage"] == "generation":
            value = {"content": "Incomplete candidate", "covered": []}
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    first = import_document(kb_dir, document(tmp_path))
    assert first.knowledge_compilation == "completed", first
    assert any(row["reason"] == "document_generation_incomplete" for row in first.omissions)
    phase = 2
    second = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert second.knowledge_compilation == "completed", second
    assert calls[(2, "planning")] == 0
    assert calls[(2, "generation")] == 1


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
    assert first.knowledge_compilation == "completed", first
    assert any(row["reason"] == "document_verification_invalid" for row in first.omissions)
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


def test_explicit_source_only_range_publishes_verified_available_content(
    kb_dir, tmp_path, monkeypatch
):
    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "planning":
            value = {
                "overview": {"text": "Alpha guidance.", "ranges": [[0, 2]], "limitations": []},
                "page_changes": [
                    {
                        "local_key": "alpha",
                        "target_key": "",
                        "kind": "concept",
                        "name": "concepts/alpha",
                        "title": "Alpha",
                        "purpose": "Alpha requirement",
                        "subject_ranges": [[0, 1]],
                        "necessary_context": [],
                    }
                ],
                "source_only": [
                    {"ranges": [[1, 2]], "reason": "The beta note remains source-only."}
                ],
                "unresolved": [],
                "resolutions": [],
            }
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, document(tmp_path))
    pages = list((kb_dir / "wiki/concepts").glob("*.md"))
    assert result.knowledge_compilation == "completed"
    assert not result.omissions
    assert [row["status"] for row in result.coverage["ranges"]] == ["verified", "no_facts"]
    assert pages


def test_explicit_request_count_stops_even_with_unlimited_tokens():
    limits = RequestLimits.from_config(
        {**DEFAULT_CONFIG, "processing": {**DEFAULT_CONFIG["processing"], "max_requests": 200}}
    )
    budget = ExecutionBudget(limits)
    budget.attempts = limits.max_requests
    with pytest.raises(ProcessingIncomplete) as failure:
        budget.reserve({"model": "openai/offline-test", "messages": []})
    assert failure.value.reason == "request_budget_exhausted"


def test_semantic_unsupported_omits_knowledge_and_finishes_publication(
    kb_dir, tmp_path, monkeypatch
):
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
    assert result.knowledge_compilation == "completed", result
    assert any(row["reason"] == "knowledge_evidence_mismatch" for row in result.omissions)
    assert calls["generation"] == 2
    # The mock correction returns the identical rejected candidate: reuse that
    # exact rejection instead of paying for another stochastic decision.
    assert calls["verification"] == 1
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_source_only_document_is_recorded_without_creating_a_page(kb_dir, tmp_path, monkeypatch):
    calls = Counter()

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls[payload["stage"]] += 1
        if payload["stage"] == "planning":
            ranges = payload["target"].get(
                "ranges",
                [[payload["target"]["target_start"], payload["target"]["target_end"]]],
            )
            return response(
                {
                    "overview": {
                        "text": "The source is retained as source-only guidance.",
                        "ranges": ranges,
                        "limitations": [],
                    },
                    "page_changes": [],
                    "source_only": [
                        {"ranges": ranges, "reason": "The note is not reusable knowledge."}
                    ],
                    "unresolved": [],
                    "resolutions": [],
                }
            )
        pytest.fail(f"Unexpected model stage: {payload['stage']}")

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(
        kb_dir,
        document(tmp_path, "The required pressure is 37 kPa. Never retry authentication failure."),
    )
    pages = list((kb_dir / "wiki/concepts").glob("*.md"))
    assert result.knowledge_compilation == "completed", result
    assert not result.omissions
    assert calls == {"planning": 1}
    assert [row["status"] for row in result.coverage["ranges"]] == ["no_facts"]
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
    from dataclasses import replace

    limits = replace(RequestLimits.from_config(settings), concurrency=1)
    calls = []
    stopped = False
    import openkb.agent.compiler as compiler

    def model(model, messages, *args, **kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append([payload["topic_labels"][member] for member in payload["topics"]])
        if not stopped and len(calls) == 2:
            raise ProcessingIncomplete("request_timeout", "planning")
        return json.dumps(evidence_response(payload))

    monkeypatch.setattr(compiler, "_llm_call", model)
    stopping_events = []
    with progress_reporting(stopping_events.append), pytest.raises(ProcessingIncomplete):
        plan_topics(
            topics,
            kb_dir,
            settings,
            limits,
            cp,
            bundle=None,
            on_event=lambda event: None,
        )
    first = calls[0]
    assert any(
        step["completed"] == len(first)
        for event in stopping_events
        for step in event["progress"]
        if step["phase"] == "planning"
    ), "Completed planning batches disappeared from the stopped progress"
    stopped = True
    calls.clear()
    events = []
    with progress_reporting(events.append):
        groups = plan_topics(
            topics,
            kb_dir,
            settings,
            limits,
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


def test_parallel_pages_finish_independent_work_and_resume_only_failed_page(
    kb_dir, tmp_path, monkeypatch
):
    import threading

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"]["concurrency"] = 4
    config_path.write_text(yaml.safe_dump(config))
    source = document(tmp_path, "\n\n".join(f"Requirement {i}." for i in range(6)))
    lock = threading.Lock()
    first = True
    calls = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "planning":
            target = payload["target"]
            start, end = target["target_start"], target["target_end"]
            value = {
                "overview": {
                    "text": "Independent document requirements.",
                    "ranges": [[start, end]],
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": f"topic-{index}",
                        "target_key": "",
                        "kind": "concept",
                        "name": f"concepts/topic-{index}",
                        "title": f"topic-{index}",
                        "purpose": f"Requirement {index}",
                        "subject_ranges": [[index, index + 1]],
                        "necessary_context": [],
                    }
                    for index in range(start, end)
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        elif payload["stage"] == "generation":
            with lock:
                title = payload["page"]["title"]
                calls.append(title)
            if first and title == "topic-0":
                value["covered"] = []
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert result.omissions[0]["reason"] == "document_generation_incomplete"
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


def test_failed_page_does_not_discard_or_prevent_later_valid_pages(kb_dir, tmp_path, monkeypatch):
    first = True
    resumed = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "planning":
            target = payload["target"]
            start, end = target["target_start"], target["target_end"]
            value = {
                "overview": {
                    "text": "Three independent requirements.",
                    "ranges": [[start, end]],
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": name.lower(),
                        "target_key": "",
                        "kind": "concept",
                        "name": f"concepts/{name.lower()}",
                        "title": name,
                        "purpose": f"{name} requirement",
                        "subject_ranges": [[index, index + 1]],
                        "necessary_context": [],
                    }
                    for index, name in enumerate(("Alpha", "Beta", "Gamma"), start=start)
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        elif payload["stage"] == "generation":
            title = payload["page"]["title"]
            if first and title == "Alpha":
                value["covered"] = []
            elif not first:
                resumed.append(title)
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(
        kb_dir, document(tmp_path, "Alpha requirement.\n\nBeta requirement.\n\nGamma requirement.")
    )
    assert result.knowledge_compilation == "completed", result
    assert result.omissions[0]["reason"] == "document_generation_incomplete"
    assert {path.stem for path in (kb_dir / "wiki/concepts").glob("*.md")} == {"beta", "gamma"}
    first = False
    result = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert result.knowledge_compilation == "completed", result
    assert resumed == ["Alpha"]


def test_document_page_correction_rechecks_original_evidence(kb_dir, tmp_path, monkeypatch):
    calls = Counter()

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stage = payload["stage"]
        calls[stage] += 1
        value = evidence_response(payload)
        if stage == "generation":
            value = {
                "content": (
                    "# Notes\nThe original requirement is preserved."
                    if payload.get("revision")
                    else "# Notes\nWrong claim."
                ),
                "covered": [item["id"] for item in payload["occurrences"]],
            }
        elif stage == "verification":
            value = {"verdict": "supported", "reason": "Correct original quote."}
            if "Wrong claim" in payload["candidate"]["content"]:
                value = {
                    "verdict": "unsupported",
                    "reason": "Correct the unsupported claim.",
                    "issues": [{"kind": "claim", "reason": "The claim is not in the source."}],
                }
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    source = document(tmp_path, "One required fact.")
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert calls == {"planning": 1, "generation": 2, "verification": 2}
    page = (kb_dir / "wiki/concepts/notes.md").read_text(encoding="utf-8")
    assert "The original requirement is preserved." in page
    assert "Wrong claim" not in page


def test_only_the_existing_correction_uses_explicit_deeper_mode(kb_dir, tmp_path, monkeypatch):
    path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings.update(compilation_thinking="disabled", correction_thinking="enabled")
    path.write_text(yaml.safe_dump(settings))
    generation_modes, review_modes = [], []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stage = payload["stage"]
        value = evidence_response(payload)
        mode = kwargs.get("extra_body", {}).get("thinking", {}).get("type")
        if stage == "generation":
            generation_modes.append(mode)
            value = {
                "content": (
                    "# Notes\nCorrect source claim."
                    if payload.get("revision")
                    else "# Notes\nWrong claim."
                ),
                "covered": [item["id"] for item in payload["occurrences"]],
            }
        elif stage == "verification":
            review_modes.append(mode)
            value = {"verdict": "supported", "reason": "Original fact preserved."}
            if "Wrong claim" in payload["candidate"]["content"]:
                value = {
                    "verdict": "unsupported",
                    "reason": "Claim absent from source.",
                    "issues": [{"kind": "claim", "reason": "Claim absent from source."}],
                }
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, document(tmp_path, "One required fact."))
    assert result.knowledge_compilation == "completed", result
    assert generation_modes == ["disabled", "enabled"]
    assert review_modes == ["disabled", "disabled"]


def test_correction_preserves_the_previously_reviewed_body(kb_dir, tmp_path, monkeypatch):
    reviewed = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "generation":
            value = {
                "content": "# Notes\nFaithful body.\n\nWrong claim.",
                "covered": [item["id"] for item in payload["occurrences"]],
            }
            if payload.get("revision"):
                value = {
                    "content": "# Notes\nFaithful body.",
                    "covered": [item["id"] for item in payload["occurrences"]],
                }
        elif payload["stage"] == "verification":
            reviewed.append(payload)
            value = {"verdict": "supported", "reason": "Original body is faithful."}
            if "Wrong claim" in payload["candidate"]["content"]:
                value = {
                    "verdict": "unsupported",
                    "reason": "Only the final claim is unsupported.",
                    "issues": [
                        {
                            "kind": "claim",
                            "candidate": "Wrong claim",
                            "occurrences": ["o1"],
                            "reason": "Unsupported final claim.",
                        }
                    ],
                }
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, document(tmp_path, "One required fact."))
    assert result.knowledge_compilation == "completed", result
    assert len(reviewed) == 2
    # Formal review sees the exact private publication proposal, including its
    # source binding and provenance markers; the generated body still carries
    # forward unchanged until the correction removes only the rejected claim.
    assert "# Notes\nFaithful body.\n\nWrong claim." in reviewed[0]["candidate"]["content"]
    assert "# Notes\nFaithful body." in reviewed[1]["candidate"]["content"]
    assert "Wrong claim" not in reviewed[1]["candidate"]["content"]
