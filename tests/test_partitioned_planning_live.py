"""Small genuine semantic sample for cross-partition identity and native scope.

Run with --test-layer live --llm-cost-report=/tmp/ultrakb-163-plan-live/cost.json.
The synthetic source and exact returned text are saved for human semantic review.
"""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from compile_work_fixtures import new_compile_work
from native_usage_fixtures import observe_executor

from ultrakb.agent.compile_capacity import CompileCapacity
from ultrakb.agent.compiler import _sanitize_concept_name
from ultrakb.agent.partition_planning import partitioned_plan
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.config import resolve_credential_bundle
from ultrakb.execution_capacity import capacity_policy
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, ModelRequest, RoleBindings
from ultrakb.locks import atomic_write_json, atomic_write_text
from ultrakb.model_tokenizer import TokenMeasure

pytestmark = pytest.mark.live


def test_real_partitioned_synonyms_and_homonyms_keep_native_locations(
    kb_dir, live_llm, monkeypatch
):
    live_llm.install(monkeypatch)
    navigation = []
    for index in range(16):
        title = (
            f"NAVIGATION_ONLY reference {index}. This source navigation label adds no "
            "independent knowledge topic. Retain the native position to find the original "
            "section later. No new page is needed for this reference heading."
        )
        if index == 1:
            title = (
                "Exponential retry backoff. Network workers wait 2, 4, 8 seconds after "
                "successive transient failures; this is the retry-interval-doubling mechanism."
            )
        elif index == 9:
            title = (
                "Retry interval doubling. Network workers wait 2, 4, 8 seconds after "
                "successive transient failures; this is the exponential-retry-backoff mechanism."
            )
        elif index == 14:
            title = (
                "Backoff in robot motion. A robot moves backwards away from a physical "
                "obstacle. This is physical reversing, unrelated to retry timing or networking."
            )
        navigation.append(
            {
                "node_id": f"section-{index:02}",
                "title": title,
                "start_index": index + 1,
                "end_index": index + 1,
            }
        )
    wiki = kb_dir / "wiki"
    atomic_write_text(
        wiki / "sources/sample.json",
        json.dumps(
            [{"page": index + 1, "content": node["title"]} for index, node in enumerate(navigation)]
        ),
    )
    atomic_write_text(wiki / "summaries/sample.md", "---\nfull_text: sources/sample.json\n---\n")
    windows = CompileSourceWindows(wiki, "sample", kb_dir=kb_dir, navigation=navigation)
    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            resolve_credential_bundle(kb_dir),
            generation={
                "compile": {"max_tokens": 1024, "reasoning_effort": "none"},
            },
        ),
        ModelCallPolicy(max_calls=12, retries=0, backoff=0, concurrency=1),
    )
    fits = CompileCapacity(
        executor,
        TokenMeasure(
            capacity_policy(
                {
                    "model": "deepseek/deepseek-flash",
                    "model_capacity": {"max_input_bytes": 5000},
                },
                custom_endpoint=False,
            )
        ),
    )
    checkpoints = new_compile_work(kb_dir, {"document": "sample"}, {"protocol": 1})
    observe_executor(kb_dir, executor, work=checkpoints)
    delivered = []
    before = len(live_llm.calls)
    budget_before = len(json.loads(Path(live_llm.budget.path).read_text())["entries"])

    def send(messages, stage):
        result = executor.complete(
            "compile",
            ModelRequest(
                messages,
                operation="compile",
                stage=stage,
                generation_options={"response_format": {"type": "json_object"}},
            ),
        )
        delivered.append(
            {
                "stage": stage,
                "text": result.text,
                "input_tokens": result.usage.prompt_tokens,
                "output_tokens": result.usage.completion_tokens,
            }
        )
        return result.text

    with executor.activate():
        plan = partitioned_plan(
            system={
                "role": "system",
                "content": (
                    "Compile source topic identities as JSON. NAVIGATION_ONLY sections need no "
                    "independent knowledge page. The two described mechanisms are concepts. "
                    "Use exact native section IDs and retain each meaning's necessary source scope."
                ),
            },
            windows=windows,
            fits=fits,
            send=send,
            entity_types=["other"],
            sanitize=_sanitize_concept_name,
            checkpoints=checkpoints,
        )
    provider_entries = json.loads(Path(live_llm.budget.path).read_text())["entries"][budget_before:]
    report = {
        "synthetic_source": navigation,
        "plan": asdict(plan),
        "delivered": delivered,
        "actual_sends": sum(entry.get("sent") is True for entry in provider_entries),
        "executor_calls": executor.calls,
        "returned_input_tokens": sum(item["input_tokens"] for item in delivered),
        "returned_output_tokens": sum(item["output_tokens"] for item in delivered),
        "actual_input_tokens": sum(
            entry.get("input_cache_hit_tokens", 0) + entry.get("input_cache_miss_tokens", 0)
            for entry in provider_entries
        ),
        "actual_output_tokens": sum(entry.get("output_tokens", 0) for entry in provider_entries),
        "actual_cost_cny": sum(entry.get("actual_nanoyuan", 0) for entry in provider_entries) / 1e9,
    }
    output = Path(live_llm.budget.path).parent / "semantic-sample.json"
    atomic_write_json(output, report)
    atomic_write_json(output.with_name(f"semantic-sample-{budget_before}.json"), report)
    assert len(plan.concepts.create) == 2 and not plan.entities.create
    scopes = [set(item["reading_locations"]["node_ids"]) for item in plan.concepts.create]
    assert any({"section-01", "section-09"} <= scope for scope in scopes)
    assert any(
        "section-14" in scope and not {"section-01", "section-09"} & scope for scope in scopes
    )
    assert sum(item["stage"] == "concepts-plan-partition" for item in delivered) >= 2
    assert executor.calls == len(live_llm.calls) - before <= 12
    # Replaying exact frozen work consumes no further model requests.
    with executor.activate():
        recovered = partitioned_plan(
            system={
                "role": "system",
                "content": (
                    "Compile source topic identities as JSON. NAVIGATION_ONLY sections need no "
                    "independent knowledge page. The two described mechanisms are concepts. "
                    "Use exact native section IDs and retain each meaning's necessary source scope."
                ),
            },
            windows=windows,
            fits=fits,
            send=send,
            entity_types=["other"],
            sanitize=_sanitize_concept_name,
            checkpoints=checkpoints,
        )
    assert recovered == plan and executor.calls == report["executor_calls"]
