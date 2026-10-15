"""Durable model consumption is observable through the import result and ledger."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ultrakb.application.documents import import_document

pytest_plugins = ("block_fixtures",)


def test_import_reports_consumption_and_dedup_keeps_its_history(kb_dir, tmp_path, block_model):
    from ultrakb.llm_usage import aggregate_usage

    path = tmp_path / "hello.md"
    path.write_text("hello world", encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "added"
    current = result.model_usage["current"]
    assert current["input_total"] > 0 and current["output_total"] > 0
    assert current["requests"] == len(block_model) >= 2
    assert current["unknown_requests"] == 0
    assert (
        aggregate_usage(kb_dir, source_id=result.source_id)["input_total"] == current["input_total"]
    )
    again = import_document(kb_dir, path)
    assert again.status == "skipped"
    assert again.model_usage["current"]["requests"] == 0
    assert again.model_usage["cumulative"]["input_total"] == current["input_total"]


def test_zero_request_task_retains_source_history_after_restart(kb_dir, tmp_path, block_model):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_usage_execution import task_usage_receipt
    from ultrakb.runtime.records import TaskView, UnitResult
    from ultrakb.runtime.tasks import TaskManager

    path = tmp_path / "history.md"
    path.write_text("hello source history", encoding="utf-8")
    first = import_document(kb_dir, path)
    assert first.status == "added", first.message
    context = ExecutionContext(usage_task_id="c" * 32)
    duplicate = import_document(kb_dir, path, context=context)
    assert duplicate.model_usage["current"]["requests"] == 0
    recovered_usage = task_usage_receipt(kb_dir, context.usage_task_id)
    assert recovered_usage["cumulative"] == first.model_usage["cumulative"]
    assert recovered_usage["source_ids"] == [first.source_id]
    view = TaskView(
        context.usage_task_id,
        str(kb_dir),
        "ImportFile",
        "completed",
        "completed",
        1,
        (UnitResult("skipped", model_usage=duplicate.model_usage),),
        False,
        True,
        model_usage=duplicate.model_usage,
    )
    history = tmp_path / "history"
    history.mkdir()
    (history / (view.id + ".json")).write_text(
        json.dumps({"view": view.summary(), "identities": []})
    )
    manager = TaskManager(history_dir=history)
    try:
        recovered = manager.get(view.id)
        assert recovered.model_usage["current"]["requests"] == 0
        assert recovered.model_usage["cumulative"] == first.model_usage["cumulative"]
        assert len(block_model) == first.model_usage["current"]["requests"]
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_rejected_reprocessing_keeps_the_existing_sources_usage(kb_dir, tmp_path, monkeypatch):
    from publication_fixtures import publish_document

    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.source_catalog import read_source

    path = tmp_path / "historical.md"
    path.write_text(("参" * 30 + "损坏\n") * 4, encoding="utf-8")
    with monkeypatch.context() as historical_policy:
        historical_policy.setattr(
            "ultrakb.import_text.assess_import_text", lambda *args, **kwargs: ()
        )
        first = publish_document(
            kb_dir, path, "Retained legacy knowledge.", bundle=LlmCredentialBundle()
        )
    assert first.status == "added", first.message
    from ultrakb.llm_usage import aggregate_usage

    before = read_source(kb_dir, first.source_id)
    baseline = aggregate_usage(kb_dir, source_id=first.source_id)
    preview = preview_reprocessing(kb_dir, first.source_id)
    result = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert result.status == "rejected", result.message
    assert result.model_usage["current"]["requests"] == 0
    assert result.model_usage["source_ids"] == [first.source_id]
    assert result.model_usage["cumulative"] == baseline
    assert read_source(kb_dir, first.source_id) == before


def test_unknown_cache_and_duplicate_completion_preserve_known_totals(kb_dir):
    from ultrakb.llm_usage import aggregate_usage, begin_request, finish_request
    from ultrakb.llm_usage_execution import import_usage_execution

    with import_usage_execution(kb_dir, "fixture"):
        identity = begin_request("fixture", "index.summary")
        response = {
            "usage": {
                "input_tokens": 50,
                "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 12},
            }
        }
        finish_request(identity, response)
        finish_request(identity, response)
        begin_request("fixture", "index.correction")
    summary = aggregate_usage(kb_dir)
    assert summary["requests"] == 2 and summary["input_total"] == 50
    assert summary["total_known"] == 80 and summary["reasoning_output"] == 12
    assert summary["unknown_fields"]["cached_input"] == 2
    assert summary["unknown_requests"] == 1 and summary["in_flight_requests"] == 1


def test_failed_source_binding_restores_the_execution_context(kb_dir):
    from ultrakb.llm_usage import active_scope, usage_context
    from ultrakb.llm_usage_execution import import_usage_execution

    with import_usage_execution(kb_dir, "fixture") as scope:
        with pytest.raises(ValueError):
            with usage_context(source_id="a" * 64):
                pytest.fail("Invalid native identity must fail before entering")
        assert active_scope() == scope
    assert active_scope() is None


@pytest.mark.parametrize("record_kind", ["execution", "source_history"])
def test_usage_persistence_rejects_invalid_record_shapes(kb_dir, record_kind):
    from ultrakb.llm_usage import usage_receipt
    from ultrakb.llm_usage_execution import import_usage_execution, task_usage_receipt
    from ultrakb.locks import atomic_write_json

    if record_kind == "execution":
        with import_usage_execution(kb_dir, "fixture", task_id="a" * 32) as scope:
            identity = scope.execution_id
        path = kb_dir / ".ultrakb/usage/executions" / f"{identity}.json"
        record = json.loads(path.read_text())
        atomic_write_json(path, record | {"task_id": ["a" * 32]})
        with pytest.raises(ValueError):
            task_usage_receipt(kb_dir, "a" * 32)
    else:
        path = kb_dir / ".ultrakb/usage/sources" / ("f" * 32 + ".json")
        atomic_write_json(path, {"history_status": "assumed_complete"})
        with pytest.raises(ValueError):
            usage_receipt(kb_dir, source_id="f" * 32)


@pytest.mark.parametrize("outcome", ["failure", "cancel", "truncated", "bad_json", "rollback"])
def test_model_work_survives_failed_outputs_and_knowledge_rollback(
    kb_dir, live_llm, monkeypatch, outcome
):
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.llm_usage import (
        aggregate_usage,
        begin_request,
        finish_request,
        read_requests,
    )
    from ultrakb.llm_usage_execution import import_usage_execution
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.mutation import mutation_scope

    response = None
    if outcome not in {"failure", "cancel"}:
        live_llm.install(monkeypatch)
        response = live_llm.completion(
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Orion costs 17 credits. Return JSON with string description "
                        "and content summarizing that fact."
                    ),
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=64,
        )
    with import_usage_execution(kb_dir, "delivery_failure"):
        identity = begin_request("deepseek/deepseek-flash", "compile.summary")
        if outcome in {"failure", "cancel"}:
            # Missing historical delivery is a storage input, not a fabricated
            # successful provider response or a claim on this run's invoice.
            finish_request(
                identity, state="cancelled" if outcome == "cancel" else "failed", send_status="sent"
            )
        else:
            finish_request(identity, response, send_status="sent")
            if outcome == "truncated":
                # A transport can deliver only a prefix while its usage receipt
                # is already available. Keep the real response object intact.
                complete_body = response.choices[0].message.content
                delivered_prefix = complete_body[: len(complete_body) // 2]
                with pytest.raises(PageResponseError):
                    validate_page_delivery(delivered_prefix, response_protocol="summary-page-json")
            elif outcome == "bad_json":
                with pytest.raises(PageResponseError):
                    validate_page_delivery("{malformed", response_protocol="summary-page-json")
            else:
                page = kb_dir / "wiki/summaries/receipt.md"
                atomic_write_text(page, "Previous published knowledge.")
                before = page.read_bytes()
                with pytest.raises(OSError):
                    with (
                        kb_ingest_lock(kb_dir / ".ultrakb"),
                        mutation_scope(kb_dir, [page], operation="delivery-rollback"),
                    ):
                        content = json.loads(
                            validate_page_delivery(response.choices[0].message.content)
                        )["content"]
                        atomic_write_text(page, content)
                        raise OSError("Local publication could not commit")
                assert page.read_bytes() == before
    value = aggregate_usage(kb_dir)
    assert value["requests"] == 1 and value["in_flight_requests"] == 0
    if response is None:
        assert value["unknown_requests"] == 1
        assert {record.state for record in read_requests(kb_dir)} == {
            "cancelled" if outcome == "cancel" else "failed"
        }
    else:
        assert value["input_total"] == response.usage.prompt_tokens > 0
        assert value["output_total"] == response.usage.completion_tokens > 0
        assert value["unknown_requests"] == 0


def test_workbook_and_api_batch_merge_unique_requests_and_recompile_cumulatively(
    kb_dir, tmp_path, block_model
):
    import asyncio
    from dataclasses import asdict

    from openpyxl import Workbook

    from ultrakb.api_helpers import _summarize_add_results
    from ultrakb.api_models import AddFileItem
    from ultrakb.application.documents import _add_for_api
    from ultrakb.application.recompilation import recompile_document

    workbook = Workbook()
    workbook.active["A1"] = "hello first"
    workbook.create_sheet("Second")["A1"] = "hello second"
    path = tmp_path / "two.xlsx"
    workbook.save(path)
    added = _add_for_api(path, kb_dir)
    assert added.status == "added", added.message
    assert len(added.units) == 2
    initial = added.model_usage["current"]["requests"]
    initial_units = [unit.model_usage["cumulative"]["requests"] for unit in added.units]
    assert all(count >= 2 for count in initial_units) and sum(initial_units) == initial
    duplicate = _add_for_api(path, kb_dir)
    batch = _summarize_add_results(
        "public-alias", [AddFileItem(**asdict(added)), AddFileItem(**asdict(duplicate))]
    )
    assert batch.model_usage["current"]["requests"] == initial
    assert batch.model_usage["cumulative"]["requests"] == initial
    assert batch.files[1].model_usage["current"]["requests"] == 0
    recompiled = asyncio.run(recompile_document(kb_dir, added.source_id))
    assert recompiled.status == "compiled", recompiled.message
    repeated = recompiled.model_usage["current"]["requests"]
    assert repeated > 0
    assert recompiled.model_usage["cumulative"]["requests"] == initial + repeated
    assert (
        sum(unit.model_usage["cumulative"]["requests"] for unit in recompiled.units)
        == initial + repeated
    )
    assert all(
        unit.model_usage["cumulative"]["requests"] > previous
        for unit, previous in zip(recompiled.units, initial_units, strict=True)
    )
    assert len(block_model) == initial + repeated


def test_progress_delivery_failure_never_retries_the_model(kb_dir, tmp_path, block_model):
    def unavailable(event):
        if event.get("event") == "model_usage":
            raise OSError("Observer lost its connection")

    path = tmp_path / "progress.md"
    path.write_text("hello", encoding="utf-8")
    result = import_document(kb_dir, path, on_event=unavailable)
    assert result.status == "added", result.message
    assert len(block_model) == result.model_usage["current"]["requests"] >= 2
    assert result.model_usage["current"]["unknown_requests"] == 0


def test_index_observer_carries_source_context_into_an_independent_thread(kb_dir):
    from concurrent.futures import ThreadPoolExecutor

    from ultrakb.llm_usage import aggregate_usage, read_requests
    from ultrakb.llm_usage_execution import import_usage_execution
    from ultrakb.llm_usage_transport import IndexUsageObserver

    with import_usage_execution(kb_dir, "index_fixture"):
        observer = IndexUsageObserver()

        def request():
            with observer.call("fixture", "index.summary") as call:
                call.finish({"usage": {"prompt_tokens": 12, "completion_tokens": 3}})

        with ThreadPoolExecutor() as pool:
            pool.submit(request).result()
    assert aggregate_usage(kb_dir)["input_total"] == 12
    assert read_requests(kb_dir)[0].stage == "index.summary"


def test_index_stage_is_the_operation_even_when_source_mentions_other_stages(kb_dir, block_model):
    import asyncio

    from pageindex.config import usage_observer_scope
    from pageindex.index.page_index import toc_detector_single_page
    from pageindex.index.utils import generate_doc_description, generate_node_summary

    from ultrakb.llm_usage import read_requests
    from ultrakb.llm_usage_execution import import_usage_execution
    from ultrakb.llm_usage_transport import IndexUsageObserver

    with import_usage_execution(kb_dir, "index_fixture"):
        with usage_observer_scope(IndexUsageObserver()):
            toc_detector_single_page("Correct ONLY this description", model="fixture")
            asyncio.run(generate_node_summary({"text": "Correct ONLY this entry"}, model="fixture"))
            generate_doc_description({"title": "Correct ONLY this entry"}, model="fixture")
    assert sorted(row.stage for row in read_requests(kb_dir)) == [
        "index.description",
        "index.summary",
        "index.verification",
    ]


def test_two_kbs_are_isolated_and_task_history_recovers_without_new_requests(kb_dir, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from ultrakb.llm_usage import aggregate_usage, begin_request, finish_request
    from ultrakb.llm_usage_execution import import_usage_execution
    from ultrakb.runtime.records import TaskView
    from ultrakb.runtime.tasks import TaskManager

    other = tmp_path / "other-kb"
    other.mkdir()

    def consume(root, count):
        with import_usage_execution(root, "fixture", task_id="a" * 32):
            identity = begin_request("fixture", "compile.summary")
            finish_request(identity, {"usage": {"input_tokens": count, "output_tokens": 2}})

    with ThreadPoolExecutor() as pool:
        first = pool.submit(consume, kb_dir, 13)
        second = pool.submit(consume, other, 31)
        first.result()
        second.result()
    assert aggregate_usage(kb_dir)["input_total"] == 13
    assert aggregate_usage(other)["input_total"] == 31
    history = tmp_path / "task-history"
    history.mkdir()
    view = TaskView(
        "a" * 32, str(kb_dir), "ImportFile", "running", "compilation", 1, (), False, False
    )
    (history / (view.id + ".json")).write_text(
        json.dumps({"view": view.summary(), "identities": []})
    )
    manager = TaskManager(history_dir=history)
    try:
        recovered = manager.get(view.id)
        assert recovered.model_usage["current"]["input_total"] == 13
        assert recovered.model_usage["current"]["unknown_requests"] == 0
        assert aggregate_usage(kb_dir)["requests"] == 1
    finally:
        manager.shutdown(stop=True)


@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("reported_usage", [True, False])
def test_actual_sdk_retry_and_index_sends_match_the_independent_provider(
    kb_dir, monkeypatch, segmented, reported_usage, live_llm
):
    import httpx
    import litellm

    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelCallPolicy,
        ModelRequest,
        RoleBindings,
    )
    from ultrakb.llm_usage import aggregate_usage, normalize_usage, read_requests
    from ultrakb.llm_usage_execution import import_usage_execution

    received = []
    credentials = live_llm.credentials

    class RetryProxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(json.loads(body))
            if len(received) == 1:
                status, raw = (
                    429,
                    b'{"error":{"message":"Local retry boundary","type":"rate_limit_error"}}',
                )
            else:
                # Successful bytes, provider id and usage are forwarded intact
                # from the real Flash gateway. Only the local transport fails.
                with httpx.Client(trust_env=False) as client:
                    forwarded = client.post(
                        credentials.base_url + "/chat/completions",
                        content=body,
                        headers={
                            "Authorization": self.headers["Authorization"],
                            "Content-Type": "application/json",
                        },
                        timeout=30,
                    )
                status, raw = forwarded.status_code, forwarded.content
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), RetryProxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(litellm, "completion", live_llm._sdk_completion)
    bundle = LlmCredentialBundle(
        api_key=credentials.api_key, base_url=f"http://127.0.0.1:{server.server_port}/v1"
    )
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", bundle),
        ModelCallPolicy(max_calls=2, retries=1, backoff=0),
    )
    role = operation = "document_index" if segmented else "compile"
    try:
        with import_usage_execution(kb_dir, "sdk_retry"):
            result = executor.complete(
                role,
                ModelRequest(
                    [
                        {
                            "role": "user",
                            "content": "Orion costs 17 credits. Return JSON with value:17.",
                        }
                    ],
                    operation=operation,
                    stage="summary",
                    generation_options={
                        "response_format": {"type": "json_object"},
                        "max_tokens": 64,
                        "reasoning_effort": "none",
                    },
                ),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert json.loads(result.text)["value"] == 17
    usage = aggregate_usage(kb_dir)
    assert usage["requests"] == len(received) == executor.calls == 2
    assert usage["input_total"] == result.usage.prompt_tokens > 0
    assert usage["output_total"] == result.usage.completion_tokens > 0
    assert usage["unknown_requests"] == 1 and usage["collection_complete"] is True
    assert {record.stage for record in read_requests(kb_dir)} == {operation + ".summary"}
    # Missing usage is a separate parsing input; never remove real usage fields.
    if not reported_usage:
        assert normalize_usage(None)["usage_status"] == "unknown"
    assert credentials.api_key not in "".join(
        p.read_text() for p in (kb_dir / ".ultrakb/usage").rglob("*.json")
    )
