"""The real PageIndex pipeline uses only its runtime injection."""

import asyncio

import pytest
from pageindex import IndexConfig, LocalClient

pytest_plugins = ("test_pdf_readback", "test_vendor_sdk")


class RecordingIndexLLM:
    def __init__(self, error=None, *, live_llm=None):
        self.calls = []
        self.error = error
        self.adapter = None
        if live_llm is not None:
            from ultrakb.index_llm import PageIndexLLM
            from ultrakb.llm_execution import CompletionExecutor, RoleBindings

            self.adapter = PageIndexLLM(
                CompletionExecutor(
                    RoleBindings(
                        "deepseek/deepseek-flash",
                        live_llm.credentials,
                        generation={"document_index": {"max_tokens": 512}},
                    )
                )
            )

    def complete(self, messages, *, stage):
        self.calls.append((stage, messages))
        if self.error:
            raise self.error
        assert self.adapter is not None, "A real model client must be injected"
        return self.adapter.complete(messages, stage=stage)

    async def acomplete(self, messages, *, stage):
        self.calls.append((stage, messages))
        if self.error:
            raise self.error
        assert self.adapter is not None, "A real model client must be injected"
        return await self.adapter.acomplete(messages, stage=stage)


def forbid_default_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Uninjected PageIndex transport")

    monkeypatch.setattr("pageindex.default_llm.DefaultIndexLLM.complete", forbidden)
    monkeypatch.setattr("pageindex.default_llm.DefaultIndexLLM.acomplete", forbidden)


@pytest.mark.asyncio
async def test_real_collection_carries_injection_across_pipeline_thread(
    tmp_path, monkeypatch, live_llm
):
    forbid_default_transport(monkeypatch)
    runtime = RecordingIndexLLM(live_llm=live_llm)
    options = IndexConfig(llm_client=runtime, require_llm_client=True, if_add_node_text=True)
    assert "llm_client" not in options.model_dump()
    assert "require_llm_client" not in options.model_dump()
    source = tmp_path / "book.md"
    source.write_text("# First\nOriginal content.\n## Second\nMore original content.")
    collection = LocalClient(
        model="openai/no-environment-key", storage_path=str(tmp_path / "db"), index_config=options
    ).collection()
    identity = collection.add(str(source))
    document = collection.get_document(identity, include_text=True)
    assert {s for s, _ in runtime.calls} == {"summary", "description"}
    assert "Original content" in str(document)
    before = len(runtime.calls)
    assert collection.add(str(source)) == identity
    assert len(runtime.calls) == before


@pytest.mark.parametrize("error", [asyncio.CancelledError(), RuntimeError("budget exhausted")])
def test_injected_terminal_errors_escape_without_index_or_retry(tmp_path, error):
    runtime = RecordingIndexLLM(error)
    source = tmp_path / "book.md"
    source.write_text("# First\nOriginal content.")
    collection = LocalClient(
        storage_path=str(tmp_path / "db"), index_config=IndexConfig(llm_client=runtime)
    ).collection()
    with pytest.raises(type(error)):
        collection.add(str(source))
    assert len(runtime.calls) == 1
    assert collection.list_documents() == []
    assert not list((tmp_path / "db/files/default").glob("*.md"))


def test_required_runtime_cannot_fall_back_to_environment(tmp_path):
    source = tmp_path / "book.md"
    source.write_text("# Title\nContent.")
    collection = LocalClient(
        storage_path=str(tmp_path / "db"), index_config=IndexConfig(require_llm_client=True)
    ).collection()
    with pytest.raises(ValueError, match="injected"):
        collection.add(str(source))


def test_ultrakb_factory_disables_the_legacy_observer():
    from ultrakb.indexer import _build_index_config

    options = _build_index_config({"model": "openai/gpt-4o"})
    assert options.llm_client is not None and options.require_llm_client
    assert options.usage_observer is None


@pytest.mark.parametrize("sdk_version", ["0.3.0.dev3+urltrakb.7", "0.3.0.dev3+urltrakb.8"])
def test_runtime_upgrade_keeps_normalization_content_identity(kb_dir, monkeypatch, sdk_version):
    from importlib import metadata

    from ultrakb.normalization import normalization_fingerprint

    original = metadata.version
    monkeypatch.setattr(
        metadata,
        "version",
        lambda name: "0.3.0.dev3+urltrakb.5" if name == "pageindex" else original(name),
    )
    before = normalization_fingerprint(kb_dir)
    monkeypatch.setattr(
        metadata,
        "version",
        lambda name: sdk_version if name == "pageindex" else original(name),
    )
    assert normalization_fingerprint(kb_dir) == before


def test_injected_pdf_structure_summary_and_description(
    tmp_path, physical_pdf, pdf_model, monkeypatch, live_llm
):
    runtime = RecordingIndexLLM(live_llm=live_llm)
    forbid_default_transport(monkeypatch)
    collection = LocalClient(
        storage_path=str(tmp_path / "index"),
        index_config=IndexConfig(
            llm_client=runtime, require_llm_client=True, if_add_node_text=True
        ),
    ).collection()
    identity = collection.add(str(physical_pdf))
    assert {s for s, _ in runtime.calls} == {"structure", "verification", "summary", "description"}
    assert [p["page"] for p in collection.get_page_content(identity, "1-3")] == [1, 2, 3]


def test_reasoning_model_drops_unsupported_temperature_per_request(live_llm):
    from litellm.llms.openai.chat.gpt_5_transformation import OpenAIGPT5Config

    from ultrakb.index_llm import PageIndexLLM
    from ultrakb.llm_execution import CompletionExecutor, RoleBindings

    adapter = PageIndexLLM(
        CompletionExecutor(
            RoleBindings(
                "deepseek/deepseek-flash",
                live_llm.credentials,
                generation={"document_index": {"max_tokens": 64}},
            )
        )
    )
    request = adapter._request([{"role": "user", "content": "Reply exactly ok."}], "structure")
    assert request.payload()["options"] == {"temperature": 0, "drop_params": True}
    prepared = OpenAIGPT5Config().map_openai_params(
        {"temperature": 0}, {}, "gpt-5", drop_params=True
    )
    assert "temperature" not in prepared
    assert adapter.complete(request.payload()["messages"], stage="structure").text.strip() == "ok"
    assert live_llm.requests[-1]["model"] == "deepseek-flash"


@pytest.mark.asyncio
async def test_unrepaired_physical_tree_cannot_be_published(tmp_path):
    from pageindex.errors import IndexQualityError
    from pageindex.index.physical_sections import require_corrected_navigation

    with pytest.raises(IndexQualityError) as failure:
        require_corrected_navigation([{"list_index": 0}])
    source = tmp_path / "book.md"
    source.write_text("# Chapter\nRetained original.")
    runtime = RecordingIndexLLM(error=failure.value)
    collection = LocalClient(
        storage_path=str(tmp_path / "db"), index_config=IndexConfig(llm_client=runtime)
    ).collection()
    with pytest.raises(IndexQualityError, match="after correction"):
        collection.add(str(source))
    assert collection.list_documents() == []
    assert not list((tmp_path / "db/files/default").glob("*.md"))
