"""Successful analysis retains its own dispatch parameters under adaptive concurrency."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import litellm
import yaml

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import profile, response


def test_larger_actual_fact_response_is_not_shared_with_a_smaller_fresh_request(
    kb_dir, tmp_path, monkeypatch
):
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["processing"] = profile(
        context_tokens=8192, output_tokens=1024, max_context_tokens=32768, max_output_tokens=4096
    )["processing"]
    path.write_text(yaml.safe_dump(config))
    first_run = True
    fact_caps = []

    def completion(**options):
        payload = json.loads(options["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            fact_caps.append(options["max_tokens"])
            if first_run and options["max_tokens"] == 1024:
                return response(truncated=True)
            quote = (
                "Voltage is 5 volts." if options["max_tokens"] == 2048 else "Timeout is 30 seconds."
            )
            for unit in value["units"]:
                unit["facts"] = [{"topic": "Operation", "statement": quote, "quote": quote}]
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    for directory in ("first", "second"):
        source = tmp_path / directory / "handbook.md"
        source.parent.mkdir()
        source.write_text("Voltage is 5 volts. Timeout is 30 seconds.")
        result = import_document(kb_dir, source)
        assert result.knowledge_compilation == "completed", result
        first_run = False
    assert fact_caps == [1024, 2048, 1024]
    records = [
        json.loads(p.read_text())
        for p in (kb_dir / ".openkb/source-store/analysis/records").glob("*.json")
    ]
    assert {
        r["input"]["effective_output_tokens"] for r in records if r["input"]["stage"] == "facts"
    } == {1024, 2048}


def test_concurrent_expansion_does_not_relabel_an_already_dispatched_response(monkeypatch):
    from openkb.agent.compiler import _llm_call
    from openkb.processing import processing_scope

    arrived, expanded = threading.Event(), threading.Event()
    calls = []

    def completion(**options):
        name = options["messages"][-1]["content"]
        cap = options["max_tokens"]
        calls.append((name, cap))
        if name == "A":
            arrived.set()
            assert expanded.wait(5)
        elif cap == 1024:
            assert arrived.wait(5)
            return response(truncated=True)
        else:
            expanded.set()
        return response({"name": name, "actual_cap": cap})

    monkeypatch.setattr(litellm, "completion", completion)
    settings = profile(output_tokens=1024, max_output_tokens=4096)
    with processing_scope(settings) as budget, ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(
                copy_context().run,
                _llm_call,
                "openai/offline-test",
                [{"role": "user", "content": name}],
                "facts",
            )
            for name in ("A", "B")
        ]
        results = [future.result(timeout=10) for future in futures]
        assert budget.limits.output_tokens == 2048
    assert [result.output_tokens for result in results] == [1024, 2048]
    assert [json.loads(result)["actual_cap"] for result in results] == [1024, 2048]
    assert sorted(calls) == [("A", 1024), ("B", 1024), ("B", 2048)]
