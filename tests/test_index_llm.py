"""The real PageIndex pipeline uses only its runtime injection."""

import asyncio

import pytest
from pageindex import IndexConfig, LocalClient
from pageindex.llm import IndexResponse

pytest_plugins = ("test_pdf_readback", "test_vendor_sdk")


class RecordingIndexLLM:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def complete(self, messages, *, stage):
        self.calls.append((stage, messages))
        if self.error:
            raise self.error
        return IndexResponse("A retained description.")

    async def acomplete(self, messages, *, stage):
        await asyncio.sleep(0)
        return self.complete(messages, stage=stage)


@pytest.mark.asyncio
async def test_real_collection_carries_injection_across_pipeline_thread(tmp_path, monkeypatch):
    import litellm

    def forbidden(**kwargs):
        pytest.fail("Uninjected PageIndex transport")

    monkeypatch.setattr(litellm, "completion", forbidden)
    monkeypatch.setattr(litellm, "acompletion", forbidden)
    runtime = RecordingIndexLLM()
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


def test_openkb_factory_disables_the_legacy_observer():
    from openkb.indexer import _build_index_config

    options = _build_index_config({"model": "openai/gpt-4o"})
    assert options.llm_client is not None and options.require_llm_client
    assert options.usage_observer is None


def test_runtime_upgrade_keeps_normalization_content_identity(kb_dir, monkeypatch):
    from importlib import metadata

    from openkb.normalization import normalization_fingerprint

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
        lambda name: "0.3.0.dev3+urltrakb.7" if name == "pageindex" else original(name),
    )
    assert normalization_fingerprint(kb_dir) == before


def test_injected_pdf_structure_summary_and_description(
    tmp_path, physical_pdf, pdf_model, monkeypatch
):
    import litellm
    from pageindex.llm import IndexResponse

    original = litellm.completion

    class PdfRuntime(RecordingIndexLLM):
        def complete(self, messages, *, stage):
            self.calls.append((stage, messages))
            return IndexResponse(original(messages=messages).choices[0].message.content)

    runtime = PdfRuntime()

    def forbidden(**kwargs):
        pytest.fail("Default PageIndex transport used")

    monkeypatch.setattr(litellm, "completion", forbidden)
    monkeypatch.setattr(litellm, "acompletion", forbidden)
    collection = LocalClient(
        storage_path=str(tmp_path / "index"),
        index_config=IndexConfig(
            llm_client=runtime, require_llm_client=True, if_add_node_text=True
        ),
    ).collection()
    identity = collection.add(str(physical_pdf))
    assert {s for s, _ in runtime.calls} == {"structure", "verification", "summary", "description"}
    assert [p["page"] for p in collection.get_page_content(identity, "1-3")] == [1, 2, 3]


def test_reasoning_model_drops_unsupported_temperature_per_request(model_service):
    from test_vendor_sdk import response

    from openkb.config import LlmCredentialBundle
    from openkb.index_llm import PageIndexLLM
    from openkb.llm_execution import CompletionExecutor, RoleBindings

    model_service.replies.append((200, response()))
    adapter = PageIndexLLM(
        CompletionExecutor(
            RoleBindings(
                "openai/gpt-5", LlmCredentialBundle(api_key="fixture", base_url=model_service.url)
            )
        )
    )
    assert adapter.complete([{"role": "user", "content": "index"}], stage="structure").text == "ok"
    assert "temperature" not in model_service.requests[0]


@pytest.mark.asyncio
async def test_unrepaired_physical_tree_cannot_be_published(monkeypatch):
    from types import SimpleNamespace

    from pageindex.errors import IndexQualityError
    from pageindex.index import page_index as pipeline

    items = [{"title": "Chapter", "physical_index": 1}]
    monkeypatch.setattr(pipeline, "process_no_toc", lambda *a, **kw: items)

    async def verify(*a, **kw):
        return 0.8, [{"list_index": 0}]

    async def unresolved(*a, **kw):
        return items, [{"list_index": 0}]

    monkeypatch.setattr(pipeline, "verify_toc", verify)
    monkeypatch.setattr(pipeline, "fix_incorrect_toc_with_retries", unresolved)
    with pytest.raises(IndexQualityError):
        await pipeline.meta_processor(
            [("Chapter", 2)], opt=IndexConfig(), logger=SimpleNamespace(info=lambda *a: None)
        )
