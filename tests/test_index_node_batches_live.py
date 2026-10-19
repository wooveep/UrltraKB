"""Paid comparison of the same required node work and frozen model settings."""

import copy
import time

import pytest
from live_llm import LiveLLM
from pageindex.index.node_batches import node_batch_scope
from pageindex.index.page_index import verify_toc
from pageindex.index.utils import generate_summaries_for_structure
from pageindex.llm import llm_client_scope

from ultrakb.execution_capacity import capacity_policy
from ultrakb.index_llm import PageIndexLLM
from ultrakb.index_stages import digest
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
from ultrakb.locks import atomic_write_json


@pytest.mark.live
@pytest.mark.asyncio
async def test_real_individual_and_batch_node_work_comparison(
    tmp_path, live_llm, monkeypatch, index_stages
):
    page_text = {
        574: "4.1 导出存储池\n4.1.3 操作步骤\n仅在主机停止后导出存储池。\n",
        575: "4.2 删除存储池\n4.2.3 操作步骤\n先备份数据；只读存储池禁止删除。\n",
        610: "6 附录\n请参照 4.3.3 操作步骤执行维护。这里没有该章节。\n",
    }
    pages = [(page_text.get(page, ""), 30) for page in range(574, 611)]
    headings = [
        {"title": "操作步骤", "structure": section, "physical_index": page}
        for section, page in [("4.1.3", 574), ("4.2.3", 575), ("4.3.3", 610)]
    ]
    summaries = [
        {
            "title": "Export",
            "start_index": 574,
            "end_index": 574,
            "text": "Export the storage pool only after the host stops. Keep its backup.",
        },
        {
            "title": "Remove",
            "start_index": 575,
            "end_index": 575,
            "text": "Back up data before removing a storage pool. Never remove a read-only pool.",
        },
        {
            "title": "Inspect",
            "start_index": 610,
            "end_index": 610,
            "text": (
                "Inspect the backup status before maintenance. If backup fails, stop maintenance."
            ),
        },
    ]
    workset = {"pages": pages, "headings": headings, "summary_nodes": summaries}
    source = tmp_path / "node-work.json"
    atomic_write_json(source, workset)
    results = []
    for batch_size in (1, 2, 8):
        client = LiveLLM(live_llm.budget, response_cache=tmp_path / f"node-arm-{batch_size}.json")
        try:
            client.install(monkeypatch)
            executor = CompletionExecutor(
                RoleBindings(
                    "deepseek/deepseek-flash",
                    client.credentials,
                    generation={
                        "document_index": {
                            "max_tokens": 1024,
                            "reasoning_effort": "none",
                        }
                    },
                ),
                ModelCallPolicy(max_calls=14, retries=0),
            )
            executor.index_capacity_policy = capacity_policy(
                {
                    "model": "deepseek/deepseek-flash",
                    "model_capacity": {
                        "max_input_bytes": 16384,
                    },
                },
                custom_endpoint=True,
            )
            runtime = PageIndexLLM(executor)
            runtime.checkpoints = index_stages(tmp_path / str(batch_size), source, identity={})
            nodes = copy.deepcopy(summaries)
            started = time.monotonic()
            with node_batch_scope(batch_size), llm_client_scope(runtime):
                accuracy, incorrect = await verify_toc(pages, headings, start_index=574)
                await generate_summaries_for_structure(nodes, model="deepseek/deepseek-flash")
                sends = len(client.receipts)
                # Replay is a semantic checkpoint read, with no additional sends.
                assert await verify_toc(pages, headings, start_index=574) == (accuracy, incorrect)
                await generate_summaries_for_structure(nodes, model="deepseek/deepseek-flash")
                assert len(client.receipts) == sends
            results.append(
                {
                    "batch_size": batch_size,
                    "workset_digest": digest(workset),
                    "wall_seconds": time.monotonic() - started,
                    "provider_sends": sends,
                    "usage": [r["usage"] for r in client.receipts],
                    "accuracy": accuracy,
                    "incorrect": incorrect,
                    "summaries": [n["summary"] for n in nodes],
                    "request_timings": client.timings,
                }
            )
        finally:
            client.close()
    atomic_write_json(
        live_llm.budget.path.with_suffix(".node-batch-comparison.json"),
        {
            "workset": workset,
            "arms": results,
            "cost": live_llm.budget.report(),
            "semantic_review": (
                "Inspect retained summaries for each source condition and exception."
            ),
        },
    )
    assert [arm["accuracy"] for arm in results] == [2 / 3, 2 / 3, 2 / 3]
    assert [[row["page_number"] for row in arm["incorrect"]] for arm in results] == [
        [610],
        [610],
        [610],
    ]
    assert results[0]["provider_sends"] == 6
    assert results[1]["provider_sends"] == 4
    assert results[2]["provider_sends"] == 2
