"""Controlled HTTP acceptance for independent consumers and interrupted executors."""

import json
import multiprocessing
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import litellm
import pytest
import yaml
from dotenv import dotenv_values

from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.agent.request_analysis import RequestAnalysis
from openkb.application.documents import import_document
from openkb.application.source_history import source_status
from openkb.cancellation import OperationCancelled, cancellation_scope
from openkb.execution_receipt import ModelText
from openkb.processing import ProcessingIncomplete, model_call, processing_scope
from openkb.runtime.requests import ContinueSource, ImportFile
from openkb.runtime.tasks import TaskManager
from openkb.sources import SourceStore
from tests.http_model_fixture import evidence_response


def analysis_for(kb_dir, settings, number):
    cp = CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id=f"{number:032x}", id=f"{number:064x}"),
        SimpleNamespace(id=f"{number + 100:064x}"),
        settings,
        None,
    )
    request = [
        {"role": "system", "content": "Return the supported verdict as JSON."},
        {"role": "user", "content": '{"stage":"verification"}'},
    ]
    return RequestAnalysis(cp, "verification", request, {})


@pytest.mark.parametrize(
    "exit_reason", ["cancel", "wait_timeout", "document_timeout", "token_budget"]
)
def test_one_waiter_leaves_other_consumers_bound_to_single_http_result(
    kb_dir, model_service, exit_reason
):
    settings = yaml.safe_load((kb_dir / ".openkb/config.yaml").read_text())
    settings["processing"]["request_timeout"] = 5
    credentials = dotenv_values(kb_dir / ".env")
    model_service.release.clear()
    stopped = threading.Event()
    started = [threading.Event(), threading.Event()]
    budgets = {}
    analyses = [analysis_for(kb_dir, settings, number) for number in (1, 2, 3)]

    def consume(index):
        config = {**settings, "processing": dict(settings["processing"])}
        if index == 1 and exit_reason == "token_budget":
            config["processing"]["max_tokens"] = 1
        elif index == 1 and exit_reason != "cancel":
            field = "request_timeout" if exit_reason == "wait_timeout" else "document_timeout"
            config["processing"][field] = 0.15
        with (
            processing_scope(config) as budget,
            cancellation_scope(stopped.is_set if index == 1 else lambda: False),
        ):
            budgets[index] = budget
            if index:
                started[index - 1].set()
            with analyses[index].pending() as raw:
                if raw is None:
                    assert index == 0, "A waiting consumer dispatched the same analysis"
                    response = model_call(
                        litellm.completion,
                        model=settings["model"],
                        messages=analyses[index].request,
                        api_key=credentials["LLM_API_KEY"],
                        api_base=credentials["OPENAI_API_BASE"],
                    )
                    raw = ModelText(response.choices[0].message.content, 1024)
                    analyses[index].save(raw)
                assert json.loads(raw)["verdict"] == "supported"
                return json.loads(raw)

    with ThreadPoolExecutor(max_workers=3) as pool:
        owner = pool.submit(consume, 0)
        try:
            assert model_service.received.wait(5)
            leaving = pool.submit(consume, 1)
            remaining = pool.submit(consume, 2)
            assert all(event.wait(5) for event in started)
            if exit_reason == "cancel":
                stopped.set()
            elif exit_reason == "token_budget":

                def forbidden(**kwargs):
                    pytest.fail("An exhausted consumer dispatched another request")

                with pytest.raises(ProcessingIncomplete, match="token_budget_exhausted"):
                    budgets[1].call(
                        forbidden, model=settings["model"], messages=analyses[1].request
                    )
            expected = OperationCancelled if exit_reason == "cancel" else ProcessingIncomplete
            with pytest.raises(expected) as error:
                leaving.result(timeout=3)
            if exit_reason != "cancel":
                assert (
                    error.value.reason
                    == {
                        "wait_timeout": "analysis_wait_timeout",
                        "document_timeout": "time_budget_exhausted",
                        "token_budget": "token_budget_exhausted",
                    }[exit_reason]
                )
            assert not owner.done() and not remaining.done()
        finally:
            model_service.release.set()
        assert owner.result(timeout=5) == remaining.result(timeout=5)

    assert len(model_service) == 1
    assert [budgets[i].attempts for i in range(3)] == [1, 0, 0]
    assert budgets[0].observations[0]["usage"] == {"input": 100, "output": 30}
    bindings = [
        json.loads(path.read_text()) for path in analyses[0].shared.root.glob("bindings/*/*.json")
    ]
    assert {row["source"]["source"] for row in bindings} == {f"{i:032x}" for i in (1, 3)}
    assert len({row["analysis"] for row in bindings}) == 1
    for index, status in ((1, "interrupted"), (2, "completed")):
        spans = budgets[index].measurement.value["spans"]
        assert any(row["stage"] == "analysis_wait" and row["status"] == status for row in spans)
    events = budgets[2].measurement.value["analyses"]
    assert {row["event"] for row in events} >= {"hit", "binding"}


def test_killed_executor_recovers_before_takeover_and_ignores_late_response(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "recover.md"
    source.write_text("Keep pressure below 37 kPa. Close the isolation valve first.")
    first_arrived, release_old, old_returned = (threading.Event() for _ in range(3))
    guard = threading.Lock()
    first = True

    def respond(body):
        nonlocal first
        with guard:
            old = first
            first = False
        if old:
            first_arrived.set()
            assert release_old.wait(30)
            old_returned.set()
            return {"units": [], "late": "Must never publish"}
        return evidence_response(json.loads(body["messages"][-1]["content"]))

    model_service.respond = respond
    manager = TaskManager(history_dir=tmp_path / "history")
    task = manager.submit(kb_dir, [ImportFile(str(source))])
    try:
        assert first_arrived.wait(15), manager.get(task)
        workers = [
            process
            for process in multiprocessing.active_children()
            if process.name.startswith(f"openkb-unit-{task[:8]}-")
        ]
        assert len(workers) == 1
        workers[0].kill()
        failed = manager.wait(task, timeout=15)
        assert failed.processes_reaped and failed.state != "completed", failed
        versions = list((kb_dir / ".openkb/source-store/versions").glob("*.json"))
        assert len(versions) == 1
        version = SourceStore(kb_dir).version(versions[0].stem)
        interrupted = source_status(kb_dir, version.source_id)
        assert interrupted["cumulative_usage"]["observable_attempts"] == 1
        assert interrupted["cumulative_usage"]["unknown_usage"] == 1
        assert not list((kb_dir / "wiki/concepts").glob("*.md"))
        resumed_task = manager.submit(
            kb_dir, [ContinueSource(version.source_id, version_id=version.id)]
        )
        resumed = manager.wait(resumed_task, timeout=20)
        assert resumed.state == "completed" and resumed.processes_reaped, resumed
        saved = resumed.results[0].document
        assert saved.knowledge_compilation == "completed"
        published = {
            path.relative_to(kb_dir): path.read_bytes()
            for path in (kb_dir / "wiki").rglob("*")
            if path.is_file()
        }
        release_old.set()
        assert old_returned.wait(5)
        assert published == {
            path.relative_to(kb_dir): path.read_bytes()
            for path in (kb_dir / "wiki").rglob("*")
            if path.is_file()
        }
        status = source_status(kb_dir, saved.source_id)
        assert status["cumulative_usage"]["observable_attempts"] == len(model_service)
        assert status["cumulative_usage"]["unknown_usage"] == 1
        before = len(model_service)
        repeated = import_document(kb_dir, source)
        assert repeated.knowledge_compilation == "completed"
        assert len(model_service) == before
        assert not list((kb_dir / ".openkb/journal").glob("*.json"))
    finally:
        release_old.set()
        manager.shutdown(stop=True)
        assert manager.join(15)


@pytest.mark.parametrize("known_usage", [True, False])
def test_shared_http_calls_are_counted_once_and_all_occurrences_are_bound(
    kb_dir, tmp_path, model_service, known_usage
):
    path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["processing"].update(
        context_tokens=4096, concurrency=8, max_tokens=1000000, max_requests=100
    )
    path.write_text(yaml.safe_dump(settings))
    source = tmp_path / "repeated.md"
    source.write_text("\n\n".join(["Pressure must remain below 37 kPa."] * 120))
    if not known_usage:
        model_service.usage = None

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "facts":
            time.sleep(0.1)
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    measured = result.usage["measurement"]
    assert len({row["id"] for row in measured["requests"]}) == len(model_service)
    assert result.usage["observable_attempts"] == len(model_service)
    status = source_status(kb_dir, result.source_id)
    assert not status["unconfirmed_requests"]
    assert status["cumulative_usage"]["observable_attempts"] == len(model_service)
    if known_usage:
        assert status["cumulative_usage"]["charged_tokens"] == len(model_service) * 130
        assert status["cumulative_usage"]["unknown_usage"] == 0
    else:
        assert status["cumulative_usage"]["unknown_usage"] == len(model_service)
        assert all(row["usage"] is None for row in result.usage["requests"])
    cited = {
        json.loads(ref)["block_id"]
        for page in (kb_dir / "wiki/concepts").glob("*.md")
        for ref in re.findall(r"<!-- source-evidence: (.*?) -->", page.read_text())
    }
    assert len(cited) == 120
    assert any(row["stage"] == "analysis_wait" for row in measured["spans"])
    assert any(row["event"] == "hit" for row in measured["analyses"])
    produced = [row["id"] for row in measured["analyses"] if row["event"] == "produced"]
    assert len(produced) == len(set(produced))
    assert len([row for row in measured["analyses"] if row["event"] == "binding"]) >= 120
