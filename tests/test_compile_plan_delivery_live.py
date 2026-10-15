"""Nonempty real-plan repair through original reads, page generation and publication.

This tests the public segmented compilation boundary, using one real text block
as its source input. It does not claim import classification or semantic index
generation coverage. No successful model result or plan is substituted.
"""

import asyncio
import copy
import hashlib
import json
from dataclasses import dataclass, field

import pytest
import yaml
from compile_plan_delivery_inputs import DOC_NAME, ORIGINAL, prepare_delivery_input

from ultrakb import frontmatter
from ultrakb.agent import compile_delivery, compile_messages, compile_plan
from ultrakb.agent.compiler import compile_long_doc
from ultrakb.agent.source_excerpts import SourceExcerpts
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.application.execution import ExecutionContext
from ultrakb.block_package import BlockPackage
from ultrakb.compilation_report import collect_compile_report
from ultrakb.llm_execution import CompletionExecutor
from ultrakb.llm_usage import read_requests
from ultrakb.locks import kb_ingest_lock

_PLAN_CONTROL = """Controlled protocol-recovery input for this small source.
For the INITIAL plan response only, return exactly {"concepts":42} as JSON.
If a later user message reports that this plan failed validation, follow the real
correction and propose exactly one new concept for the Orion retry policy, with
no entity/create/update/related proposals besides that single concept create.
Use the supplied original navigation to provide relevant native reading_locations
and evidence_units. Select the original block coordinates or supplied node_ids;
do not invent character offsets. Preserve the source's conditions and exact
configuration field. Return the complete corrected concepts/entities plan.
"""


@dataclass
class DeliveryObservation:
    requests: list[dict] = field(default_factory=list)
    delivered: list[tuple] = field(default_factory=list)
    plans: list = field(default_factory=list)
    reads: list[dict] = field(default_factory=list)
    source_checks: list[dict] = field(default_factory=list)
    publications: list[dict] = field(default_factory=list)


def observe_delivery(monkeypatch):
    observation = DeliveryObservation()
    planning = compile_messages.planning_messages
    request_plan = compile_plan.request_compile_plan
    complete = CompletionExecutor.complete
    acomplete = CompletionExecutor.acomplete
    prepare = CompileSourceWindows.prepare
    response = SourceExcerpts.response
    publish = compile_delivery.publish_compile_results

    def planning_input(*args, **kwargs):
        # This is the only fault injection: actual input sent to the real model.
        # It is included before the production request digest is computed.
        assert kwargs.get("long") is True
        return [*planning(*args, **kwargs), {"role": "user", "content": _PLAN_CONTROL}]

    def observe_plan(*args, **kwargs):
        actual = request_plan(*args, **kwargs)
        observation.plans.append(copy.deepcopy(actual))
        return actual

    def before_request(executor, role, request):
        payload = request.payload()
        generated = {**executor.bindings.generation_options(role), **payload["options"]}
        assert 0 < generated["max_tokens"] <= 768
        assert executor.policy.max_calls == 5 and executor.policy.retries == 0
        assert executor.policy.deadline is not None
        assert 0 < executor.bindings.credentials().timeout <= 30
        observation.requests.append({"role": role, "stage": request.stage, **payload})

    def observe_sync(executor, role, request, **kwargs):
        before_request(executor, role, request)
        actual = complete(executor, role, request, **kwargs)
        observation.delivered.append((request.stage, actual))
        return actual

    async def observe_async(executor, role, request, **kwargs):
        before_request(executor, role, request)
        actual = await acomplete(executor, role, request, **kwargs)
        observation.delivered.append((request.stage, actual))
        return actual

    def observe_read(reader, item, previous_page=None, *, long=False):
        actual = prepare(reader, item, previous_page, long=long)
        if reader.doc_name == DOC_NAME:
            observation.reads.append(
                {
                    "item": copy.deepcopy(item),
                    "long": long,
                    "windows": copy.deepcopy(actual.windows),
                }
            )
        return actual

    def observe_source_check(excerpts, *args, **kwargs):
        actual = response(excerpts, *args, **kwargs)
        observation.source_checks.append(
            {"protocol": kwargs.get("protocol"), "sources": tuple(excerpts.path_sources)}
        )
        return actual

    def observe_publish(*args, **kwargs):
        actual = publish(*args, **kwargs)
        observation.publications.append(
            {
                "plan": copy.deepcopy(args[2]),
                "concept_results": copy.deepcopy(kwargs.get("concept_results", ())),
            }
        )
        return actual

    monkeypatch.setattr(compile_messages, "planning_messages", planning_input)
    monkeypatch.setattr(compile_plan, "request_compile_plan", observe_plan)
    monkeypatch.setattr(CompletionExecutor, "complete", observe_sync)
    monkeypatch.setattr(CompletionExecutor, "acomplete", observe_async)
    monkeypatch.setattr(CompileSourceWindows, "prepare", observe_read)
    monkeypatch.setattr(SourceExcerpts, "response", observe_source_check)
    monkeypatch.setattr(compile_delivery, "publish_compile_results", observe_publish)
    return observation


@pytest.mark.integration
def test_plan_delivery_input_has_real_block_origins_and_no_test_tokenizer(tmp_path):
    source = prepare_delivery_input(tmp_path)
    package = BlockPackage.model_validate_json(source.package.read_text())
    assert package.source.text == source.original.read_text() == ORIGINAL
    assert (
        package.source.original_digest == hashlib.sha256(source.original.read_bytes()).hexdigest()
    )
    assert len(package.source.blocks) == len(source.navigation) == 1
    assert source.navigation[0]["start_index"] == source.navigation[0]["end_index"] == 1
    assert not source.summary.exists()
    assert not list((source.wiki / "concepts").glob("*.md"))
    persisted = yaml.safe_load((source.kb / ".ultrakb/config.yaml").read_text())
    assert "model_capacity" not in persisted and "tokenizer_model" not in persisted


@pytest.mark.live
def test_real_nonempty_plan_repair_reads_original_then_generates_and_publishes(
    tmp_path, live_llm, monkeypatch
):
    live_llm.install(monkeypatch)
    source = prepare_delivery_input(tmp_path)
    frozen_path = source.retained.with_suffix(".content.json")
    frozen_bytes = frozen_path.read_bytes()
    before = len(live_llm.calls)
    observed = observe_delivery(monkeypatch)
    context = ExecutionContext()
    with (
        kb_ingest_lock(source.kb / ".ultrakb"),
        context.begin(source.kb) as bundle,
        collect_compile_report() as report,
    ):
        asyncio.run(
            compile_long_doc(
                DOC_NAME,
                source.summary,
                "native-block-input",
                source.kb,
                "deepseek/deepseek-flash",
                bundle=bundle,
                max_concurrency=1,
                navigation=source.navigation,
            )
        )

    plan_deliveries = [value for stage, value in observed.delivered if stage == "concepts-plan"]
    assert len(plan_deliveries) == 2
    assert json.loads(plan_deliveries[0].text) == {"concepts": 42}
    plan_requests = [value for value in observed.requests if value["stage"] == "concepts-plan"]
    assert "previous JSON plan failed validation" in json.dumps(plan_requests[1]["messages"])
    assert len(observed.plans) == 1
    plan = observed.plans[0]
    assert len(plan.concepts.create) == 1
    assert not plan.concepts.update and not plan.concepts.related
    assert not plan.entities.create and not plan.entities.update and not plan.entities.related
    item = plan.concepts.create[0]
    assert item["evidence_units"] == [
        1
    ]  # Preserved legacy field; native reads use reading_locations.
    assert item["reading_locations"]

    assert len(observed.reads) == 1 and observed.reads[0]["long"] is True
    windows = observed.reads[0]["windows"]
    assert windows and all(window["coverage"] == "complete" for window in windows)
    assert all(window["sha256"] == hashlib.sha256(frozen_bytes).hexdigest() for window in windows)
    assert all(
        window["path"] == frozen_path.relative_to(source.wiki).as_posix() for window in windows
    )
    assert ORIGINAL.strip() in "".join(window["content"] for window in windows)
    writing = [value for value in observed.requests if value["stage"].startswith("concept: ")]
    assert 1 <= len(writing) <= 2
    assert all(ORIGINAL.strip() in json.dumps(value["messages"]) for value in writing)
    assert any(
        ORIGINAL.strip() in original
        for check in observed.source_checks
        for original in check["sources"]
    )

    assert len(observed.publications) == 1
    assert len(observed.publications[0]["concept_results"]) == 1
    concept_path = source.wiki / "concepts" / f"{item['name']}.md"
    assert set((source.wiki / "concepts").glob("*.md")) == {concept_path}
    content = concept_path.read_text()
    assert "Orion" in content and "retry_limit = 17" in content
    assert frontmatter.parse(content)["sources"] == [f"summaries/{DOC_NAME}.md"]
    assert (
        frontmatter.parse(source.summary.read_text())["full_text"]
        == frozen_path.relative_to(source.wiki).as_posix()
    )
    assert f"[[concepts/{item['name']}]]" in source.summary.read_text()
    assert f"[[concepts/{item['name']}]]" in (source.wiki / "index.md").read_text()
    assert frozen_path.read_bytes() == frozen_bytes
    assert "compile_plan_repaired" in report.quality and not report.unfinished

    records = read_requests(source.kb)
    assert 4 <= len(records) == len(observed.delivered) == len(live_llm.calls) - before <= 5
    assert all(
        record.send_status == "sent" and record.usage_status == "known" for record in records
    )
    assert all(result.raw_usage_available for _, result in observed.delivered)
