"""Public import seam for the document-plan compilation path."""

from __future__ import annotations

import json

import yaml

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from openkb.application.source_artifacts import compilation_artifacts


def test_import_generates_from_planned_occurrences_not_facts(kb_dir, tmp_path, model_service):
    source = tmp_path / "planned.md"
    source.write_text("Install the service.\n\nDo not run it before configuration.")

    result = import_document(kb_dir, source)

    assert result.knowledge_compilation == "completed", result
    payloads = [json.loads(request["messages"][-1]["content"]) for request in model_service]
    assert [payload["stage"] for payload in payloads] == [
        "planning",
        "generation",
        "verification",
    ]
    generation = payloads[1]
    assert generation["page"]["name"] == "concepts/notes"
    assert generation["occurrences"]
    assert "facts" not in generation
    plan_path = next(
        (kb_dir / ".openkb" / "source-store" / "compilation" / "recovery").glob("*-plan.json")
    )
    page = json.loads(plan_path.read_text())["value"]["pages"][0]
    assert page["quality"] == "published"
    assert page["review_receipt"]["verdict"] == "supported"
    plan = json.loads(plan_path.read_text())["value"]
    receipt = plan["metadata"]["accepted_window_receipts"][0]
    assert receipt["checkpoint"] and len(receipt["result"]) == 64
    assert all(len(receipt[key]) == 64 for key in ("request", "predecessor", "delta"))
    measurement = result.usage["measurement"]
    assert measurement["schema"] == 3
    planning = measurement["document_planning"]
    assert [row["event"] for row in planning] == ["accepted"]
    observation = planning[0]
    assert observation["target_ranges"] and observation["result"] == receipt["result"]
    assert observation["frozen_prefix_blocks"] > 0 and len(observation["frozen_prefix_hash"]) == 64
    assert observation["candidate_count"] == 1
    request = next(row for row in measurement["requests"] if row["id"] == observation["request_id"])
    assert observation["input_reservation"] == request["input_reservation"]
    assert observation["actual_input"] == request["input_tokens"] == 100
    assert observation["actual_output"] == request["output_tokens"] == 30
    summary = measurement["summary"]
    assert summary["request_p50_seconds"] is not None
    assert summary["request_p95_seconds"] is not None
    assert summary["input_tokens_total"] == 300
    assert summary["output_tokens_total"] == 90
    assert summary["peak_rss_bytes"] is None or summary["peak_rss_bytes"] > 0
    summary_page = next((kb_dir / "wiki" / "summaries").glob("planned-*.md")).read_text()
    assert "Overview of document knowledge." not in summary_page


def test_import_keeps_character_range_coverage_exact(kb_dir, tmp_path, model_service):
    source = tmp_path / "partial.md"
    source.write_text("abcdefghij")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "planning":
            return {
                "overview": {
                    "text": "A partial source plan.",
                    "ranges": [[0, 1]],
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "p",
                        "target_key": "",
                        "kind": "concept",
                        "name": "concepts/first-half",
                        "title": "First half",
                        "purpose": "The first selected excerpt",
                        "subject_ranges": [{"block_index": 0, "start_char": 0, "end_char": 5}],
                        "necessary_context": [],
                    }
                ],
                "source_only": [
                    {
                        "ranges": [{"block_index": 0, "start_char": 5, "end_char": 10}],
                        "reason": "The trailing literal remains with the source entry.",
                    }
                ],
                "unresolved": [],
                "resolutions": [],
            }
        if payload["stage"] == "generation":
            return {"content": "# First half\nabcde", "covered": ["o1"]}
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "The excerpt is preserved."}
        raise AssertionError(payload["stage"])

    model_service.respond = respond
    result = import_document(kb_dir, source)

    assert result.knowledge_compilation == "completed", result
    ranges = result.coverage["ranges"]
    assert [(row["start"], row["end"], row["status"]) for row in ranges] == [
        (0, 5, "verified"),
        (5, 10, "no_facts"),
    ]


def test_none_review_mode_keeps_a_private_draft_out_of_public_pages(
    kb_dir, tmp_path, model_service
):
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["review_mode"] = "none"
    config_path.write_text(yaml.safe_dump(config))
    source = tmp_path / "draft.md"
    source.write_text("A draftable source statement.")

    result = import_document(kb_dir, source)

    assert result.knowledge_compilation == "unfinished", result
    assert result.reason == "draft_unverified"
    assert not [
        request
        for request in model_service
        if '"stage":"verification"' in request["messages"][-1]["content"]
    ]
    assert not list((kb_dir / "wiki" / "concepts").glob("*.md"))
    assert any(item["reason"] == "draft_unverified" for item in result.omissions)
    assert list(
        (kb_dir / ".openkb" / "source-store" / "compilation" / "recovery").glob("*-draft.json")
    )


def test_none_review_mode_never_retracts_an_existing_source_contribution(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "preserved.md"
    source.write_text("The first version is verified and published.")
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    page = kb_dir / "wiki" / "concepts" / "notes.md"
    before = page.read_text(encoding="utf-8")

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["review_mode"] = "none"
    config_path.write_text(yaml.safe_dump(config))
    source.write_text("The replacement is intentionally left as an unverified draft.")

    draft = import_document(kb_dir, source)

    assert draft.reason == "draft_unverified"
    assert page.read_text(encoding="utf-8") == before


def test_plan_only_stops_before_generation_and_exposes_the_private_plan(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "plan-only.md"
    source.write_text("A source statement that should be planned but not published.")

    result = import_document(kb_dir, source, plan_only=True)

    assert result.status == "unfinished"
    assert result.stage == "planned"
    assert result.reason == "document_plan_ready"
    assert [
        json.loads(request["messages"][-1]["content"])["stage"] for request in model_service
    ] == ["planning"]
    assert not list((kb_dir / "wiki" / "concepts").glob("*.md"))
    artifacts = compilation_artifacts(
        kb_dir, result.source_id, result.input_version, result.parse_id, "planning"
    )
    assert artifacts["total"] >= 1
    plan_text = next(
        record["text"] for record in artifacts["records"] if "计划状态：accepted" in record["text"]
    )
    assert "notes" in plan_text


def test_plan_only_reports_a_recoverable_planning_failure_as_pending(
    kb_dir, tmp_path, model_service
):
    """A placeholder omission must not be presented as a formal ready plan."""

    source = tmp_path / "plan-only-invalid.md"
    source.write_text("A source whose planner response is intentionally invalid.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        assert payload["stage"] == "planning"
        return {"not": "a valid document plan"}

    model_service.respond = respond
    result = import_document(kb_dir, source, plan_only=True)

    assert result.status == "unfinished"
    assert result.stage == "planning"
    assert result.reason == "document_plan_invalid"
    assert result.reason != "document_plan_ready"
    assert not list((kb_dir / "wiki" / "concepts").glob("*.md"))


def test_critical_review_reuses_none_mode_candidate_and_only_reviews_it(
    kb_dir, tmp_path, model_service
):
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["review_mode"] = "none"
    config_path.write_text(yaml.safe_dump(config))
    source = tmp_path / "upgrade-review.md"
    source.write_text("A source statement awaiting critical review.")

    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "unfinished", first
    first_request_count = len(model_service)

    config["review_mode"] = "critical"
    config_path.write_text(yaml.safe_dump(config))
    second = continue_source(kb_dir, first.source_id, version_id=first.input_version)

    assert second.knowledge_compilation == "completed", second
    payloads = [
        json.loads(request["messages"][-1]["content"])
        for request in model_service[first_request_count:]
    ]
    assert [payload["stage"] for payload in payloads] == ["verification"]
    assert list((kb_dir / "wiki" / "concepts").glob("*.md"))


def test_post_publish_receipt_failure_stays_recoverable_until_continue_repairs_it(
    kb_dir, tmp_path, model_service, monkeypatch
):
    import openkb.agent.document_publication as publication

    source = tmp_path / "receipt-repair.md"
    source.write_text("A published page must retain its formal plan receipt.")
    original = publication.record_document_publication

    def fail_receipt(*_args, **_kwargs):
        raise OSError("simulated receipt write failure")

    monkeypatch.setattr(publication, "record_document_publication", fail_receipt)
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "unfinished"
    assert first.reason == "document_publication_receipt_pending"
    assert list((kb_dir / "wiki" / "concepts").glob("*.md"))
    plan_path = next(
        (kb_dir / ".openkb" / "source-store" / "compilation" / "recovery").glob("*-plan.json")
    )
    pending = json.loads(plan_path.read_text())["value"]["metadata"]
    assert "publication_pending" in pending and "publication_receipt" not in pending

    monkeypatch.setattr(publication, "record_document_publication", original)
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["language"] = "zh"
    config_path.write_text(yaml.safe_dump(config))
    requests_before = len(model_service)
    repaired = continue_source(kb_dir, first.source_id, version_id=first.input_version)

    assert repaired.knowledge_compilation == "completed"
    assert len(model_service) == requests_before
    settled = json.loads(plan_path.read_text())["value"]
    assert "publication_pending" not in settled["metadata"]
    assert settled["metadata"]["publication_receipt"]["proposal_id"]
    assert settled["pages"][0]["quality"] == "published"


def test_continue_preserves_a_partial_publication_and_retries_only_its_omission(
    kb_dir, tmp_path, model_service, monkeypatch
):
    """A terminal planning replay cannot lose an earlier proposal's page proof."""

    from openkb.agent import document_pages
    from openkb.cancellation import OperationCancelled
    from tests.http_model_fixture import evidence_response

    source = tmp_path / "partial-retry.md"
    source.write_text("First published fact.\n\nSecond fact awaiting a retry.")
    retry_second = False

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "planning":
            target = payload["target"]
            ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
            return {
                "overview": {
                    "text": "Two independently planned facts.",
                    "ranges": ranges,
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "first",
                        "target_key": "",
                        "kind": "concept",
                        "name": "concepts/first",
                        "title": "First",
                        "purpose": "The first source paragraph.",
                        "subject_ranges": [[0, 1]],
                        "necessary_context": [],
                    },
                    {
                        "local_key": "second",
                        "target_key": "",
                        "kind": "concept",
                        "name": "concepts/second",
                        "title": "Second",
                        "purpose": "The second source paragraph.",
                        "subject_ranges": [[1, 2]],
                        "necessary_context": [],
                    },
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        if payload["stage"] == "generation":
            page = payload["page"]["name"]
            if page == "concepts/second" and not retry_second:
                # The response deliberately fails validation, leaving a
                # recoverable omission while the first page is published.
                return {"content": "# Second\nnot accepted", "covered": []}
            return {
                "content": "# " + payload["page"]["title"] + "\nverified",
                "covered": [item["id"] for item in payload["occurrences"]],
            }
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "Exact source evidence is preserved."}
        return evidence_response(payload)

    model_service.respond = respond
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    assert first.coverage["status"] == "partial"

    original_generate = document_pages.generate_document_page
    monkeypatch.setattr(
        document_pages,
        "generate_document_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationCancelled()),
    )
    interrupted = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert interrupted.status == "stopped"

    plan_path = next(
        (kb_dir / ".openkb" / "source-store" / "compilation" / "recovery").glob("*-plan.json")
    )
    paused = json.loads(plan_path.read_text(encoding="utf-8"))["value"]
    assert paused["metadata"]["publication_receipt"]["proposal_id"]
    assert paused["pages"][0]["quality"] == "published"
    assert "concepts/first.md" in paused["metadata"]["publication_page_receipts"]

    monkeypatch.setattr(document_pages, "generate_document_page", original_generate)
    retry_second = True
    from openkb.agent import document_publication

    calls_before = len(model_service)
    from openkb.application.execution import ExecutionContext

    events = []
    original_record = document_publication.record_document_publication
    monkeypatch.setattr(
        document_publication,
        "record_document_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("post-commit receipt interruption")
        ),
    )
    post_commit_failure = continue_source(
        kb_dir,
        first.source_id,
        version_id=first.input_version,
        context=ExecutionContext(on_event=events.append),
    )

    assert post_commit_failure.reason == "document_publication_receipt_pending"
    retry_generations = [
        json.loads(request["messages"][-1]["content"])["page"]["name"]
        for request in model_service[calls_before:]
        if json.loads(request["messages"][-1]["content"])["stage"] == "generation"
    ]
    assert retry_generations == ["concepts/second"]
    reuse_events = [
        event for event in events if event.get("operation", "").startswith("published_page_")
    ]
    assert {
        event["page"] for event in reuse_events if event.get("operation") == "published_page_reused"
    } == {"concepts/first"}, reuse_events
    pending = json.loads(plan_path.read_text(encoding="utf-8"))["value"]
    assert pending["metadata"]["publication_receipt"]["proposal_id"]
    assert pending["metadata"]["publication_pending"]["proposal_id"]
    assert pending["pages"][0]["quality"] == "published"

    # P2 is already atomically published but its recorder was interrupted.
    # Repair must apply P2's matching pending receipt instead of rejecting the
    # stale P1 receipt retained for the first page.
    monkeypatch.setattr(document_publication, "record_document_publication", original_record)
    repair_calls_before = len(model_service)
    completed = continue_source(kb_dir, first.source_id, version_id=first.input_version)

    assert completed.knowledge_compilation == "completed", completed
    assert len(model_service) == repair_calls_before
    settled = json.loads(plan_path.read_text(encoding="utf-8"))["value"]
    assert {page["quality"] for page in settled["pages"]} == {"published"}
    assert set(settled["metadata"]["publication_page_receipts"]) == {
        "concepts/first.md",
        "concepts/second.md",
    }


def test_lost_formal_plan_receipt_never_falls_back_to_legacy_publication(
    kb_dir, tmp_path, model_service, monkeypatch
):
    import openkb.agent.document_publication as publication

    source = tmp_path / "lost-receipt.md"
    source.write_text("The committed proposal needs a durable formal receipt.")
    original = publication.record_document_publication
    monkeypatch.setattr(
        publication,
        "record_document_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("receipt write failed")),
    )
    first = import_document(kb_dir, source)
    assert first.reason == "document_publication_receipt_pending"
    plan_path = next(
        (kb_dir / ".openkb" / "source-store" / "compilation" / "recovery").glob("*-plan.json")
    )
    intent = next(
        (kb_dir / ".openkb" / "source-store" / "compilation" / "publication-intents").glob("*.json")
    )
    assert json.loads(intent.read_text())["status"] == "pending"
    plan_path.unlink()
    monkeypatch.setattr(publication, "record_document_publication", original)
    requests_before = len(model_service)

    blocked = continue_source(kb_dir, first.source_id, version_id=first.input_version)

    assert blocked.knowledge_compilation == "unfinished"
    assert blocked.reason == "document_publication_receipt_pending"
    assert len(model_service) == requests_before
