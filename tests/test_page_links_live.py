"""Paid identity semantics smoke sample, separate from offline coverage and recovery."""

import json

import pytest

from ultrakb.agent.compile_capacity import CompileCapacity
from ultrakb.agent.compiler import _llm_call_page_async
from ultrakb.agent.link_registry import FrozenLinkRegistry, LinkTarget
from ultrakb.execution_capacity import capacity_policy
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
from ultrakb.locks import atomic_write_json
from ultrakb.model_tokenizer import TokenMeasure


@pytest.mark.live
@pytest.mark.asyncio
async def test_real_model_resolves_same_named_software_and_memory_cache(live_llm, monkeypatch):
    live_llm.install(monkeypatch)
    registry = FrozenLinkRegistry(
        [
            LinkTarget("concepts/cpu-cache", ("Cache", "CPU cache", "processor memory cache")),
            LinkTarget(
                "entities/cache-manager", ("Cache", "Cache replication software"), "software"
            ),
            *(LinkTarget(f"concepts/unrelated-{number}") for number in range(100)),
        ]
    )
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", live_llm.credentials),
        ModelCallPolicy(max_calls=4, retries=0, concurrency=1),
    )
    measure = TokenMeasure(
        capacity_policy(
            {"model": "deepseek/deepseek-flash", "model_capacity": {"max_input_bytes": 16000}},
            custom_endpoint=True,
        )
    )
    sizer = CompileCapacity(executor, measure)
    before = len(live_llm.receipts)
    original = {
        "description": "Use [[Cache|the replication software]] to manage replication jobs.",
        "content": "The CPU keeps recently accessed instructions in [[Cache|its memory cache]].",
    }
    with executor.activate():
        delivered = await _llm_call_page_async(
            "deepseek/deepseek-flash",
            [
                {"role": "system", "content": "Return the supported page as JSON."},
                registry.message("CPU memory cache and Cache replication software"),
            ],
            "link-identity-sample",
            bundle=live_llm.credentials,
            response_format={"type": "json_object"},
            max_tokens=256,
            link_registry=registry,
            request_fits=lambda messages, options: sizer(messages, options),
            initial_response=json.dumps(original),
        )
    result = json.loads(delivered)
    assert "[[entities/cache-manager|the replication software]]" in result["description"]
    assert "[[concepts/cpu-cache|its memory cache]]" in result["content"]
    receipts = live_llm.receipts[before:]
    atomic_write_json(
        live_llm.budget.path.with_suffix(".links-validation.json"),
        {
            "purpose": "Small same-name software/concept identity semantics sample",
            "catalogue_size": len(registry.targets),
            "result": result,
            "provider_sends": len(receipts),
            "usage": [receipt["usage"] for receipt in receipts],
            "cost": live_llm.budget.report(),
        },
    )
    assert 0 < len(receipts) <= 4


@pytest.mark.live
@pytest.mark.asyncio
async def test_real_unknown_synonym_resolves_against_the_frozen_full_catalogue(
    live_llm, monkeypatch
):
    live_llm.install(monkeypatch)
    registry = FrozenLinkRegistry(
        [
            LinkTarget(
                "concepts/cpu-cache",
                ("CPU cache",),
                "",
                "Small fast memory close to the CPU that holds recently used instruction bytes.",
            ),
            LinkTarget(
                "concepts/disk-cache",
                ("Operating system disk cache",),
                "",
                "Memory used by an operating system to cache file-system disk blocks.",
            ),
            LinkTarget(
                "entities/replication-manager",
                ("Replication manager",),
                "software",
                "Software that manages network data replication jobs.",
            ),
        ]
    )
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", live_llm.credentials),
        ModelCallPolicy(max_calls=4, retries=0, concurrency=1),
    )
    sizer = CompileCapacity(
        executor,
        TokenMeasure(
            capacity_policy(
                {"model": "deepseek/deepseek-flash", "model_capacity": {"max_input_bytes": 16000}},
                custom_endpoint=True,
            )
        ),
    )
    before = len(live_llm.receipts)
    original = {
        "description": "CPU instruction memory.",
        "content": "Recently executed instruction bytes are kept close to the CPU in "
        "[[processor scratch store|that fast memory]].",
    }
    with executor.activate():
        delivered = await _llm_call_page_async(
            "deepseek/deepseek-flash",
            [{"role": "system", "content": "Return supported page fields as JSON."}],
            "unknown-link-synonym-sample",
            bundle=live_llm.credentials,
            response_format={"type": "json_object"},
            max_tokens=256,
            link_registry=registry,
            request_fits=lambda messages, options: sizer(messages, options),
            initial_response=json.dumps(original),
        )
    result = json.loads(delivered)
    assert "[[concepts/cpu-cache|that fast memory]]" in result["content"]
    receipts = live_llm.receipts[before:]
    atomic_write_json(
        live_llm.budget.path.with_suffix(".unknown-link-validation.json"),
        {
            "purpose": "Unknown synonym requires semantic matching of the full frozen catalogue",
            "catalogue_size": len(registry.targets),
            "result": result,
            "provider_sends": len(receipts),
            "usage": [receipt["usage"] for receipt in receipts],
            "cost": live_llm.budget.report(),
        },
    )
    assert 0 < len(receipts) <= 4
