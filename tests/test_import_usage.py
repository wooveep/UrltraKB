"""Durable model consumption is observable through the import result and ledger."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openkb.application.documents import import_document

pytest_plugins = ("block_fixtures",)


def test_import_reports_consumption_and_dedup_keeps_its_history(kb_dir, tmp_path, block_model):
    from openkb.llm_usage import aggregate_usage

    path = tmp_path / "hello.md"
    path.write_text("hello world", encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "added"
    assert result.model_usage["current"]["input_total"] == 2
    assert result.model_usage["current"]["output_total"] == 2
    assert result.model_usage["current"]["requests"] == 2
    assert aggregate_usage(kb_dir, source_id=result.source_id)["input_total"] == 2
    again = import_document(kb_dir, path)
    assert again.status == "skipped"
    assert again.model_usage["current"]["requests"] == 0
    assert again.model_usage["cumulative"]["input_total"] == 2


def test_unknown_cache_and_duplicate_completion_preserve_known_totals(kb_dir):
    from openkb.llm_usage import aggregate_usage, begin_request, finish_request
    from openkb.llm_usage_execution import import_usage_execution

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
    from openkb.llm_usage import active_scope, usage_context
    from openkb.llm_usage_execution import import_usage_execution

    with import_usage_execution(kb_dir, "fixture") as scope:
        with pytest.raises(ValueError):
            with usage_context(source_id="a" * 64):
                pytest.fail("Invalid native identity must fail before entering")
        assert active_scope() == scope
    assert active_scope() is None


@pytest.mark.parametrize("outcome", ["failure", "cancel", "truncated", "bad_json", "rollback"])
def test_model_work_survives_failed_outputs_and_knowledge_rollback(
    kb_dir, tmp_path, block_model, monkeypatch, outcome
):
    import litellm

    from openkb.llm_usage import aggregate_usage, read_requests
    from openkb.locks import LockCancelled
    from openkb.mutation import MutationSnapshot

    original = litellm.completion

    def completion(**kwargs):
        if outcome == "failure":
            raise ConnectionError("Fixture disconnected after sending")
        if outcome == "cancel":
            raise LockCancelled("Fixture cancelled after sending")
        response = original(**kwargs)
        if outcome == "truncated":
            response.choices[0].finish_reason = "length"
        elif outcome == "bad_json" and len(block_model) > 1:
            response.choices[0].message.content = "{malformed"
        return response

    monkeypatch.setattr(litellm, "completion", completion)
    if outcome == "rollback":
        commit = MutationSnapshot.mark_committed

        def lost_commit(snapshot):
            if snapshot.operation == "compile-import-unit":
                raise OSError("Fixture rolled back knowledge")
            commit(snapshot)

        monkeypatch.setattr(MutationSnapshot, "mark_committed", lost_commit)
    path = tmp_path / "failure.md"
    path.write_text("hello world", encoding="utf-8")
    if outcome == "cancel":
        with pytest.raises(LockCancelled):
            import_document(kb_dir, path)
    else:
        result = import_document(kb_dir, path)
        assert result.model_usage["current"]["requests"] > 0
        if outcome in {"failure", "rollback"}:
            assert result.status == "failed"
        elif outcome == "bad_json":
            assert result.quality and result.unfinished
    value = aggregate_usage(kb_dir)
    assert value["requests"] > 0
    if outcome in {"failure", "cancel"}:
        assert value["unknown_requests"] == value["requests"]
        assert value["in_flight_requests"] == 0
        assert {r.state for r in read_requests(kb_dir)} == {
            "cancelled" if outcome == "cancel" else "failed"
        }
    else:
        assert value["input_total"] == len(block_model)
        assert value["output_total"] == len(block_model)
        assert value["unknown_requests"] == 0


def test_workbook_and_api_batch_merge_unique_requests_and_recompile_cumulatively(
    kb_dir, tmp_path, block_model
):
    import asyncio
    from dataclasses import asdict

    from openpyxl import Workbook

    from openkb.api_helpers import _summarize_add_results
    from openkb.api_models import AddFileItem
    from openkb.application.documents import _add_for_api
    from openkb.application.recompilation import recompile_document

    workbook = Workbook()
    workbook.active["A1"] = "hello first"
    workbook.create_sheet("Second")["A1"] = "hello second"
    path = tmp_path / "two.xlsx"
    workbook.save(path)
    added = _add_for_api(path, kb_dir)
    assert added.status == "added", added.message
    assert len(added.units) == 2
    assert added.model_usage["current"]["requests"] == 4
    assert [unit.model_usage["cumulative"]["requests"] for unit in added.units] == [2, 2]
    duplicate = _add_for_api(path, kb_dir)
    batch = _summarize_add_results(
        "public-alias", [AddFileItem(**asdict(added)), AddFileItem(**asdict(duplicate))]
    )
    assert batch.model_usage["current"]["requests"] == 4
    assert batch.model_usage["cumulative"]["requests"] == 4
    assert batch.files[1].model_usage["current"]["requests"] == 0
    recompiled = asyncio.run(recompile_document(kb_dir, added.source_id))
    assert recompiled.status == "compiled", recompiled.message
    assert recompiled.model_usage["current"]["requests"] == 4
    assert recompiled.model_usage["cumulative"]["requests"] == 8
    assert [unit.model_usage["cumulative"]["requests"] for unit in recompiled.units] == [4, 4]


def test_progress_delivery_failure_never_retries_the_model(kb_dir, tmp_path, block_model):
    def unavailable(event):
        if event.get("event") == "model_usage":
            raise OSError("Fixture observer lost its connection")

    path = tmp_path / "progress.md"
    path.write_text("hello", encoding="utf-8")
    result = import_document(kb_dir, path, on_event=unavailable)
    assert result.status == "added", result.message
    assert len(block_model) == result.model_usage["current"]["requests"] == 2


def test_index_observer_carries_source_context_into_an_independent_thread(kb_dir):
    from concurrent.futures import ThreadPoolExecutor

    from openkb.llm_usage import aggregate_usage, read_requests
    from openkb.llm_usage_execution import import_usage_execution
    from openkb.llm_usage_transport import IndexUsageObserver

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

    from openkb.llm_usage import read_requests
    from openkb.llm_usage_execution import import_usage_execution
    from openkb.llm_usage_transport import IndexUsageObserver

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

    from openkb.llm_usage import aggregate_usage, begin_request, finish_request
    from openkb.llm_usage_execution import import_usage_execution
    from openkb.runtime.records import TaskView
    from openkb.runtime.tasks import TaskManager

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
    kb_dir, tmp_path, monkeypatch, segmented, reported_usage
):
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.config import LlmCredentialBundle

    sent = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            sent.append(body)
            if len(sent) == 1:
                status = 429
                response = {"error": {"message": "Retry fixture", "type": "rate_limit_error"}}
            else:
                status = 200
                prompt = str(body["messages"])
                content = {
                    "description": "Fixture",
                    "content": "A greeting",
                    "create": [],
                    "update": [],
                    "related": [],
                }
                if "CONTENT BLOCK STRUCTURE" in prompt:
                    content = [
                        {
                            "structure": "1",
                            "title": "Greeting",
                            "title_origin": "original",
                            "physical_index": 1,
                            "anchor": {
                                "unit": 1,
                                "part": "body",
                                "range": [0, 5],
                                "excerpt": "hello",
                            },
                        }
                    ]
                response = {
                    "id": f"provider-{len(sent)}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(content)},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 4},
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    },
                }
                if not reported_usage:
                    del response["usage"]
            raw = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setenv("LLM_API_KEY", "fixture-secret")
    monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1")
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "model": "gpt-4o",
                **(
                    {"model_capacity": {"max_input_tokens": 1, "tokenizer_model": "gpt-4o"}}
                    if segmented
                    else {}
                ),
            },
        ),
    )
    path = tmp_path / "network.md"
    path.write_text("hello world", encoding="utf-8")
    try:
        result = import_document(
            kb_dir,
            path,
            bundle=LlmCredentialBundle(
                api_key="fixture-secret", base_url=f"http://127.0.0.1:{server.server_port}/v1"
            ),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.status == "added", result.message
    value = result.model_usage["current"]
    assert value["requests"] == len(sent)
    assert value["input_total"] == ((len(sent) - 1) * 10 if reported_usage else 0)
    assert value["output_total"] == ((len(sent) - 1) * 5 if reported_usage else 0)
    assert value["cached_input"] == ((len(sent) - 1) * 4 if reported_usage else 0)
    assert value["reasoning_output"] == ((len(sent) - 1) * 2 if reported_usage else 0)
    assert value["unknown_requests"] == (1 if reported_usage else len(sent))
    assert value["collection_complete"] is True
    assert result.units[0].model_usage["current"]["requests"] == len(sent)
    if segmented:
        assert any(stage.startswith("index.") for stage in value["stages"])
    assert "fixture-secret" not in "".join(
        p.read_text() for p in (kb_dir / ".openkb/usage").rglob("*.json")
    )
