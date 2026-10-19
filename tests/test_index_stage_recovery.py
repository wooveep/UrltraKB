"""Real SDK pipeline: bounded final navigation and response-level crash recovery."""

import json

import litellm
import pytest
from pageindex import IndexConfig, LocalClient

from ultrakb.execution_capacity import capacity_policy, request_input
from ultrakb.index_llm import PageIndexLLM
from ultrakb.llm_execution import CompletionExecutor, LlmCredentialBundle, RoleBindings
from ultrakb.locks import LockCancelled
from ultrakb.model_tokenizer import ContextCapacityError

pytestmark = pytest.mark.integration


def response(text):
    return litellm.ModelResponse(
        choices=[{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )


def runtime(root, source, index_stages, limit=4096):
    executor = CompletionExecutor(
        RoleBindings("openai/fixture", LlmCredentialBundle(), model_table={})
    )
    executor.index_capacity_policy = capacity_policy(
        {"model": "openai/fixture", "model_capacity": {"max_input_bytes": limit}},
        custom_endpoint=True,
    )
    adapter = PageIndexLLM(executor)
    adapter.checkpoints = index_stages(root, source, identity={"model": "fixture"})
    return adapter


def collection(root, adapter):
    client = LocalClient(
        storage_path=str(root / "index"),
        index_config=IndexConfig(
            model="gpt-4o",
            llm_client=adapter,
            require_llm_client=True,
            max_concurrency=1,
            node_batch_size=1,
            if_add_node_text=True,
        ),
    )
    return client, client.collection()


@pytest.mark.parametrize("limit", [2200, 4096, 9000])
def test_many_small_nodes_use_bounded_description_and_resume_only_missing_work(
    tmp_path, monkeypatch, limit, index_stages
):
    source = tmp_path / "source.md"
    source.write_text("".join(f"# Chapter {n}\nOriginal section {n}.\n\n" for n in range(158)))
    sent = []

    async def summary(**kwargs):
        sent.append(("summary", kwargs))
        return response("中" * 200)

    def description(**kwargs):
        sent.append(("description", kwargs))
        return response("完整文档的导航描述。")

    monkeypatch.setattr(litellm, "acompletion", summary)
    monkeypatch.setattr(litellm, "completion", description)
    first = runtime(tmp_path, source, index_stages, limit=limit)
    save = first.checkpoints.save

    def interrupt(key, result):
        save(key, result)
        if key.startswith("response:") and sum(s == "summary" for s, _ in sent) == 7:
            first.executor.cancel()

    monkeypatch.setattr(first.checkpoints, "save", interrupt)
    client, col = collection(tmp_path, first)
    with pytest.raises(LockCancelled):
        col.add(str(source))
    assert col.list_documents() == []
    assert sum(s == "summary" for s, _ in sent) == 7
    client.close()

    # More capacity changes partition sizes, not accepted source/summary identity.
    second = runtime(tmp_path, source, index_stages, limit=limit * 2)
    client, col = collection(tmp_path, second)
    identity = col.add(str(source))
    document = col.get_document(identity, include_text=True)
    assert len(document["structure"]) == 158
    assert all(n["summary"] == "中" * 200 for n in document["structure"])
    assert sum(s == "summary" for s, _ in sent) == 158
    assert second.checkpoints.reused >= 7
    assert sum(s == "description" for s, _ in sent) > 1
    assert all(
        len(json.dumps(request_input(kwargs), ensure_ascii=False).encode()) <= limit * 2
        for _, kwargs in sent
    )
    assert document["metadata"]["doc_description_status"] == "completed"
    client.close()


def test_optional_description_failure_retains_a_complete_document(
    tmp_path, monkeypatch, index_stages
):
    source = tmp_path / "source.md"
    source.write_text("# Chapter\nThe retained original evidence.\n")

    async def summary(**kwargs):
        return response("Navigation summary.")

    monkeypatch.setattr(litellm, "acompletion", summary)
    adapter = runtime(tmp_path, source, index_stages)
    original = adapter.complete

    def complete(messages, *, stage):
        if stage == "description":
            raise ContextCapacityError("Injected metadata capacity conflict")
        return original(messages, stage=stage)

    monkeypatch.setattr(adapter, "complete", complete)
    client, col = collection(tmp_path, adapter)
    identity = col.add(str(source))
    document = col.get_document(identity, include_text=True)
    assert document["structure"]
    assert "retained original evidence" in document["structure"][0]["text"]
    assert document["metadata"]["doc_description_status"] == "pending"
    client.close()


@pytest.mark.asyncio
async def test_large_correction_partitions_native_pages_before_sending(
    tmp_path, monkeypatch, index_stages
):
    from pageindex.index.page_index import single_toc_item_index_fixer
    from pageindex.llm import llm_client_scope

    source = tmp_path / "source.md"
    source.write_text("Original evidence")
    adapter = runtime(tmp_path, source, index_stages, limit=2200)
    sent = []

    async def locate(**kwargs):
        sent.append(kwargs)
        text = kwargs["messages"][-1]["content"].split("Document pages:\n", 1)[1]
        number = "<physical_index_2>" if "TARGET" in text else None
        return response(json.dumps({"physical_index": number}))

    monkeypatch.setattr(litellm, "acompletion", locate)
    pages = (
        "<physical_index_1>\n" + "native first page\n" * 200 + "<physical_index_1>\n"
        "<physical_index_2>\n" + "native second page\n" * 100 + "TARGET\n<physical_index_2>"
    )
    with llm_client_scope(adapter, required=True):
        assert await single_toc_item_index_fixer("TARGET", pages, model="gpt-4o") == 2
    assert len(sent) > 1
    assert all(len(json.dumps(request_input(k), ensure_ascii=False).encode()) <= 2200 for k in sent)
