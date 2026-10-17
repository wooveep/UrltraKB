"""Per-node receipts and recovery at PageIndex's injected model boundary."""

import copy
import json

import pytest
from pageindex.index.page_index import check_title_appearance_in_start_concurrent, verify_toc
from pageindex.index.utils import generate_summaries_for_structure
from pageindex.llm import IndexResponse, llm_client_scope


class BatchPort:
    """Protocol fixtures exercise identity checks, never semantic quality."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        self.saved = {}

    def complete(self, messages, *, stage):
        raise AssertionError("This node work is asynchronous")

    async def acomplete(self, messages, *, stage):
        prompt = messages[-1]["content"]
        payload = json.loads(prompt.split("\nNode work:\n", 1)[1])
        self.requests.append(payload)
        return IndexResponse(json.dumps(self.reply(payload, len(self.requests))))

    def stage_result(self, key, value=None):
        if value is None:
            return copy.deepcopy(self.saved.get(key))
        self.saved[key] = copy.deepcopy(value)

    def fits(self, messages, *, stage):
        return True


@pytest.mark.asyncio
async def test_shared_page_verification_keeps_a_receipt_for_every_node():
    pages = [("1 Install\nPrepare the host.\n2 Remove\nStop the service.\n", 20)]
    nodes = [
        {"title": "Install", "structure": "1", "physical_index": 1},
        {"title": "Remove", "structure": "2", "physical_index": 1},
    ]
    client = BatchPort(
        lambda payload, _: {
            "results": [{"id": node["id"], "answer": "yes"} for node in payload["nodes"]]
        }
    )
    with llm_client_scope(client):
        accuracy, incorrect = await verify_toc(pages, nodes)
        assert await verify_toc(pages, nodes) == (accuracy, incorrect)
    assert (accuracy, incorrect) == (1.0, [])
    assert len(client.requests) == 1
    assert len(client.requests[0]["nodes"]) == 2
    assert len(client.requests[0]["contexts"]) == 1
    assert len([key for key in client.saved if key.startswith("node-work:")]) == 2


@pytest.mark.asyncio
async def test_complete_requests_split_before_the_model_boundary():
    class BoundedPort(BatchPort):
        def fits(self, messages, *, stage):
            payload = json.loads(messages[-1]["content"].split("\nNode work:\n", 1)[1])
            return len(payload["nodes"]) <= 2

    nodes = [{"title": f"Topic {i}", "physical_index": 1} for i in range(5)]
    client = BoundedPort(
        lambda payload, _: {
            "results": [{"id": node["id"], "answer": "yes"} for node in payload["nodes"]]
        }
    )
    with llm_client_scope(client):
        assert await verify_toc([("Native page text", 20)], nodes) == (1.0, [])
    assert sum(len(request["nodes"]) for request in client.requests) == 5
    assert max(len(request["nodes"]) for request in client.requests) <= 2


@pytest.mark.asyncio
async def test_out_of_range_ids_are_rejected_before_acceptance():
    def reply(payload, attempt):
        rows = [{"id": node["id"], "answer": "yes"} for node in payload["nodes"]]
        if attempt == 1:
            rows.append({"id": "not-a-requested-node", "answer": "yes"})
        return {"results": rows}

    client = BatchPort(reply)
    with llm_client_scope(client):
        assert await verify_toc(
            [("Native page text", 20)],
            [{"title": "First", "physical_index": 1}, {"title": "Second", "physical_index": 1}],
        ) == (1.0, [])
    assert len(client.requests) == 2
    assert len(client.requests[1]["nodes"]) == 2
    assert len([key for key in client.saved if key.startswith("node-work:")]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "duplicate", "bad_answer"])
async def test_only_the_failed_receipt_is_repaired(fault):
    pages = [("1 Install\nPrepare.\n2 Remove\nStop.\n", 20)]
    nodes = [
        {"title": "Install", "structure": "1", "physical_index": 1},
        {"title": "Remove", "structure": "2", "physical_index": 1},
    ]

    def reply(payload, attempt):
        rows = [{"id": node["id"], "answer": "yes"} for node in payload["nodes"]]
        if attempt == 1:
            if fault == "missing":
                rows.pop()
            elif fault == "duplicate":
                rows.append(rows[-1])
            else:
                rows[-1]["answer"] = "probably"
        return {"results": rows}

    client = BatchPort(reply)
    with llm_client_scope(client):
        assert await verify_toc(pages, nodes) == (1.0, [])
    first, repair = client.requests
    assert [n["id"] for n in repair["nodes"]] == [first["nodes"][1]["id"]]
    assert len([key for key in client.saved if key.startswith("node-work:")]) == 2


@pytest.mark.asyncio
async def test_summaries_attach_by_id_when_the_model_returns_reversed_receipts():
    shared = "Service maintenance requires a stopped host."
    nodes = [
        {"title": "Maintenance", "line_num": 1, "text": shared},
        {"title": "Maintenance", "line_num": 8, "text": shared},
    ]

    def reply(payload, _):
        first, second = payload["nodes"]
        return {
            "results": [
                {"id": second["id"], "summary": "Receipt for the second location."},
                {"id": first["id"], "summary": "Receipt for the first location."},
            ]
        }

    client = BatchPort(reply)
    with llm_client_scope(client):
        await generate_summaries_for_structure(nodes)
        await generate_summaries_for_structure(nodes)
    assert [node["summary"] for node in nodes] == [
        "Receipt for the first location.",
        "Receipt for the second location.",
    ]
    assert len(client.requests) == 1
    assert len(client.requests[0]["contexts"]) == 1
    assert client.requests[0]["nodes"][0]["id"] != client.requests[0]["nodes"][1]["id"]


@pytest.mark.asyncio
async def test_ambiguous_page_starts_keep_separate_native_locations():
    pages = [("Introduction\nResume\nBody.\n", 10), ("Resume\nOther body.\n", 10)]
    nodes = [{"title": "Resume", "physical_index": page} for page in (1, 2)]

    def reply(payload, _):
        return {
            "results": [
                {"id": node["id"], "start_begin": "yes" if node["physical_index"] == 2 else "no"}
                for node in reversed(payload["nodes"])
            ]
        }

    client = BatchPort(reply)
    with llm_client_scope(client):
        await check_title_appearance_in_start_concurrent(nodes, pages)
    assert [node["appear_start"] for node in nodes] == ["no", "yes"]
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_exhausted_local_repair_retains_unaffected_receipts_for_resume():
    from pageindex.index.node_batches import NodeBatchError

    nodes = [{"title": title, "physical_index": 1} for title in ("First", "Second")]

    def missing(payload, _):
        return {
            "results": [
                {"id": node["id"], "answer": "yes"}
                for node in payload["nodes"]
                if node["title"] == "First"
            ]
        }

    client = BatchPort(missing)
    with llm_client_scope(client):
        with pytest.raises(NodeBatchError, match="repair exhausted") as raised:
            await verify_toc([("Original text", 20)], nodes)
        assert raised.value.local_recovery_exhausted
        assert len([key for key in client.saved if key.startswith("node-work:")]) == 1
        client.reply = lambda payload, _: {
            "results": [{"id": node["id"], "answer": "yes"} for node in payload["nodes"]]
        }
        assert await verify_toc([("Original text", 20)], nodes) == (1.0, [])
    assert len(client.requests) == 4
    assert [node["title"] for node in client.requests[-1]["nodes"]] == ["Second"]


@pytest.mark.asyncio
async def test_removing_an_unrelated_candidate_keeps_native_node_receipts():
    nodes = [{"title": title, "physical_index": 1} for title in ("First", "Second")]
    client = BatchPort(
        lambda payload, _: {
            "results": [{"id": node["id"], "answer": "yes"} for node in payload["nodes"]]
        }
    )
    with llm_client_scope(client):
        assert await verify_toc([("Frozen original text", 20)], nodes) == (1.0, [])
        assert await verify_toc([("Frozen original text", 20)], nodes[1:]) == (1.0, [])
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_summary_receipts_follow_native_ranges_when_the_workset_changes():
    nodes = [
        {"title": "First", "line_num": 1, "text": "First original."},
        {"title": "Second", "line_num": 8, "text": "Second original."},
    ]
    client = BatchPort(
        lambda payload, _: {
            "results": [
                {"id": node["id"], "summary": "A retained native-node summary."}
                for node in payload["nodes"]
            ]
        }
    )
    with llm_client_scope(client):
        await generate_summaries_for_structure(nodes)
        await generate_summaries_for_structure(nodes[1:])
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_scheduler_changes_reuse_receipts_but_source_or_contract_changes_do_not(monkeypatch):
    from pageindex.config import IndexConfig, llm_params_scope
    from pageindex.index import node_batches

    nodes = [{"title": title, "physical_index": 1} for title in ("First", "Second")]
    client = BatchPort(
        lambda payload, _: {
            "results": [{"id": node["id"], "answer": "yes"} for node in payload["nodes"]]
        }
    )
    assert (
        IndexConfig(node_batch_size=1).model_dump() == IndexConfig(node_batch_size=8).model_dump()
    )
    with llm_client_scope(client):
        assert await verify_toc([("Original text", 20)], nodes) == (1.0, [])
        original_ids = [node["id"] for node in client.requests[-1]["nodes"]]
        with node_batches.node_batch_scope(1):
            assert await verify_toc([("Original text", 20)], nodes) == (1.0, [])
        assert len(client.requests) == 1
        assert await verify_toc([("Changed source", 20)], nodes) == (1.0, [])
        assert original_ids != [node["id"] for node in client.requests[-1]["nodes"]]
        with llm_params_scope({"temperature": 1}):
            assert await verify_toc([("Original text", 20)], nodes) == (1.0, [])
        assert len(client.requests) == 3
        monkeypatch.setattr(node_batches, "CONTRACT", "next-accepted-node-contract")
        assert await verify_toc([("Original text", 20)], nodes) == (1.0, [])
        assert len(client.requests) == 4


@pytest.mark.parametrize(("first_size", "resume_size"), [(8, 1), (1, 8)])
def test_delivered_work_survives_cancel_before_validation_and_rescheduling(
    tmp_path, monkeypatch, first_size, resume_size
):
    import litellm
    from pageindex import IndexConfig, LocalClient

    from ultrakb.execution_capacity import capacity_policy
    from ultrakb.index_llm import PageIndexLLM
    from ultrakb.index_stages import IndexStages
    from ultrakb.llm_execution import CompletionExecutor, LlmCredentialBundle, RoleBindings
    from ultrakb.locks import LockCancelled

    source = tmp_path / "source.md"
    source.write_text("".join(f"# Section {i}\nOriginal topic {i}.\n\n" for i in range(8)))
    sent = []

    async def send(**kwargs):
        prompt = kwargs["messages"][-1]["content"]
        if "\nNode work:\n" in prompt:
            payload = json.loads(prompt.split("\nNode work:\n", 1)[1])
            content = json.dumps(
                {
                    "results": [
                        {"id": node["id"], "summary": "Summary for " + node["title"]}
                        for node in payload["nodes"]
                    ]
                }
            )
        else:
            title = (
                prompt.split("Partial Document Text: ", 1)[1].split("\n", 1)[0].removeprefix("# ")
            )
            content = "Summary for " + title
        sent.append(kwargs)
        return litellm.ModelResponse(
            choices=[
                {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    monkeypatch.setattr(litellm, "acompletion", send)

    def runtime():
        executor = CompletionExecutor(
            RoleBindings("openai/fixture", LlmCredentialBundle(), model_table={})
        )
        executor.index_capacity_policy = capacity_policy(
            {"model": "openai/fixture", "model_capacity": {"max_input_bytes": 9000}},
            custom_endpoint=True,
        )
        adapter = PageIndexLLM(executor)
        adapter.checkpoints = IndexStages(tmp_path, source, identity={"model": "fixture"})
        return adapter

    first = runtime()
    save = first.checkpoints.save

    def cancel_after_delivery(key, value):
        save(key, value)
        if key.startswith("response:"):
            first.executor.cancel()

    monkeypatch.setattr(first.checkpoints, "save", cancel_after_delivery)
    options = {"if_add_doc_description": False, "if_add_node_text": True}
    with LocalClient(
        storage_path=str(tmp_path / "index"),
        index_config=IndexConfig(llm_client=first, node_batch_size=first_size, **options),
    ) as client:
        with pytest.raises(LockCancelled):
            client.collection().add(str(source))
        assert client.collection().list_documents() == []
    assert len(sent) == 1

    with LocalClient(
        storage_path=str(tmp_path / "index"),
        index_config=IndexConfig(llm_client=runtime(), node_batch_size=resume_size, **options),
    ) as client:
        collection = client.collection()
        document = collection.get_document(collection.add(str(source)), include_text=True)
    assert [node["summary"] for node in document["structure"]] == [
        f"Summary for Section {i}" for i in range(8)
    ]
    assert len(sent) == (1 if first_size == 8 else 2)
    if first_size == 1:
        remaining = json.loads(sent[-1]["messages"][-1]["content"].split("\nNode work:\n", 1)[1])
        assert [node["title"] for node in remaining["nodes"]] == [
            f"Section {i}" for i in range(1, 8)
        ]
