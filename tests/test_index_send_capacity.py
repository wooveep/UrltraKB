"""Complete indexing requests must fit before any real SDK transport attempt."""

import asyncio
import copy
import json

import litellm
import pytest
from error_transport import ErrorTransport

from ultrakb import config
from ultrakb.application.connection_capacity import template_capacity
from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.indexer import _build_index_config
from ultrakb.locks import kb_ingest_lock
from ultrakb.model_tokenizer import ContextCapacityError

pytestmark = pytest.mark.integration


@pytest.fixture
def index_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "profile/global.yaml")
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    kb = tmp_path / "kb"
    with ErrorTransport(status=401) as service:
        initialize_kb(
            kb,
            model="deepseek/deepseek-flash",
            api_key="offline-index-capacity-key",
            openai_api_base=service.url,
            seed_environment=False,
        )
        values = config.load_config(kb / ".ultrakb/config.yaml")
        values.update(
            model_capacity={**template_capacity(CONNECTION_TEMPLATES[2]), "max_input_tokens": 96},
            model_policy={"max_calls": 8, "retries": 2, "backoff": 0},
        )
        config.save_config(kb / ".ultrakb/config.yaml", values)
        yield kb, service


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "stage", ["structure", "verification", "correction", "summary", "description"]
)
async def test_chinese_index_input_counts_message_envelope_before_transport(
    index_connection, asynchronous, stage
):
    kb, service = index_connection
    text = "检修前关闭进水阀，检查密封后重新开启。"
    assert len(text.encode("utf-8")) < 96
    context = ExecutionContext()
    with kb_ingest_lock(kb / ".ultrakb"), context.begin(kb) as bundle:
        later = config.resolve_effective_config(kb)[0]
        # A later factory input cannot enlarge this execution's captured 96 units.
        later["model_capacity"]["max_input_tokens"] = 65536
        runtime = _build_index_config(later, bundle=bundle).llm_client
        with pytest.raises(ContextCapacityError):
            messages = [{"role": "user", "content": text}]
            if asynchronous:
                await runtime.acomplete(messages, stage=stage)
            else:
                runtime.complete(messages, stage=stage)
        assert context.executor.calls == 0
        assert context.executor.budget.snapshot()["pending"] == 0
    assert service.requests == []


@pytest.mark.parametrize("known", [True, False])
def test_direct_index_factory_enforces_capacity_without_an_active_execution(
    index_connection, known
):
    kb, service = index_connection
    values = config.resolve_effective_config(kb)[0]
    if not known:
        values.pop("model_capacity")
    runtime = _build_index_config(values, bundle=config.resolve_credential_bundle(kb)).llm_client
    with pytest.raises(ContextCapacityError, match="capacity|tokenizer"):
        runtime.complete(
            [{"role": "user", "content": "检修前关闭进水阀，检查密封后重新开启。"}],
            stage="structure",
        )
    assert runtime.executor.calls == 0
    assert service.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["continuation", "correction", "chat_history", "repair_history"]
)
async def test_complete_vendor_index_request_respects_exact_capacity_and_one_unit_overflow(
    index_connection, operation
):
    from pageindex.index import page_index
    from pageindex.index.utils import llm_completion
    from pageindex.llm import llm_client_scope

    kb, service = index_connection
    original = "1 检修步骤\n关闭进水阀，检查密封后重新开启。"
    navigation = [{"structure": "1", "title": "检修步骤", "physical_index": 1}]
    pages = [(original, 20)]
    historical_messages = [
        {"role": "user", "content": original},
        {"role": "assistant", "content": json.dumps(navigation, ensure_ascii=False)},
    ]
    before = copy.deepcopy((navigation, pages, historical_messages))

    async def request(runtime):
        with llm_client_scope(runtime, required=True):
            if operation == "continuation":
                await asyncio.to_thread(
                    page_index.generate_toc_continue,
                    navigation,
                    "<physical_index_2>\n2 复核\n确认阀门状态。",
                    "deepseek/deepseek-flash",
                )
            elif operation == "correction":
                await page_index.fix_incorrect_toc_with_retries(
                    navigation,
                    pages,
                    [{"list_index": 0, "title": "检修步骤"}],
                    model="deepseek/deepseek-flash",
                )
            else:
                prompt = (
                    "Correction required: 原文标题范围不符，不能把页眉视为章节。"
                    "仅返回新条目，保留先前已确认的结构。"
                    if operation == "repair_history"
                    else "请继续目录，保留原文顺序。"
                )
                await asyncio.to_thread(
                    llm_completion,
                    "deepseek/deepseek-flash",
                    prompt,
                    chat_history=historical_messages,
                )

    async def attempt(limit, error):
        values = config.load_config(kb / ".ultrakb/config.yaml")
        values["model_capacity"]["max_input_tokens"] = limit
        values["role_generation"] = {
            "document_index": {"max_tokens": 1024, "reasoning_effort": "none"}
        }
        config.save_config(kb / ".ultrakb/config.yaml", values)
        count = len(service.requests)
        context = ExecutionContext()
        with kb_ingest_lock(kb / ".ultrakb"), context.begin(kb) as bundle:
            runtime = _build_index_config(values, bundle=bundle).llm_client
            with pytest.raises(error):
                await request(runtime)
            sent = 0 if error is ContextCapacityError else 1
            assert context.executor.calls == sent
            assert context.executor.budget.snapshot()["pending"] == 0
        assert len(service.requests) == count + sent

    # The real SDK/local 401 exit reveals the request, without any success reply.
    await attempt(5000, litellm.AuthenticationError)
    wire = service.requests[-1]["json"]
    expected = {
        "messages": wire["messages"],
        "temperature": 0,
        "max_tokens": 1024,
        "reasoning_effort": "none",
    }
    assert all(wire[key] == value for key, value in expected.items())
    if operation in {"chat_history", "repair_history"}:
        assert wire["messages"][:2] == historical_messages
        if operation == "repair_history":
            assert "Correction required" in wire["messages"][-1]["content"]
    elif operation == "continuation":
        assert "Previous tree structure" in wire["messages"][0]["content"]
        assert json.dumps(navigation, indent=2) in wire["messages"][0]["content"]
    else:
        assert original in wire["messages"][0]["content"]
    # Independent UTF-8 count for the actual no-merge ByteLevel template.
    required = len(json.dumps(expected, ensure_ascii=False).encode("utf-8"))
    assert len(original.encode("utf-8")) < required < 5000
    await attempt(required, litellm.AuthenticationError)
    await attempt(required - 1, ContextCapacityError)
    assert (navigation, pages, historical_messages) == before
