"""New pages size the final input, retain every original, and resume accepted parts."""

import json
from types import SimpleNamespace

import pytest


def creation_inputs(kb, *, kind="concepts", limit=9000):
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.compile_checkpoints import CompileCheckpoints

    wiki = kb / "wiki"
    text = "\n\n".join(f"Operation {i}: retain its independent condition." for i in range(160))
    text += "\n\nTAIL: the final exception must remain covered.\n"
    (wiki / "sources/manual.md").write_text(text)
    reader = CompileSourceWindows(wiki, "manual", kb_dir=kb)
    originals = reader.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(text)}"}}, long=True
    )
    cp = CompileCheckpoints(
        kb,
        SimpleNamespace(task_id="a" * 32, parent_task_id=None),
        {"document": "manual"},
        {"protocol": "retained-create-test"},
    )
    options = dict(
        windows=reader,
        originals=originals,
        model="unused",
        bundle=None,
        system_message={"role": "system", "content": "Compile grounded knowledge."},
        current_original=None,
        known_targets_message={"role": "user", "content": "rules"},
        task_message={"role": "user", "content": "Write a page with description and content."},
        page=f"{kind}/rules.md",
        checkpoints=cp,
        fits=lambda messages: len(json.dumps(messages, ensure_ascii=False).encode()) < limit,
    )
    return options, text, cp


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["concepts", "entities"])
async def test_create_splits_complete_input_and_resumes_at_first_missing_part(
    kb_dir, monkeypatch, kind
):
    from ultrakb.agent import compiler
    from ultrakb.agent.create_generation import prepare_page_creation

    options, text, cp = creation_inputs(kb_dir, kind=kind)
    prepared = prepare_page_creation(**options)
    assert len(prepared.batches) > 1
    assert "".join(w["content"] for b in prepared.batches for w in b.windows) == text
    calls = []

    async def delivered(model, messages, stage, **kwargs):
        calls.append(stage)
        if len(calls) == 2:
            raise RuntimeError("Interrupted after one accepted contribution")
        assert options["fits"](messages)
        return json.dumps(
            {
                "description": "Document rules.",
                "type": "other",
                "content": "# Rules\n\nSupported rule.",
                "covered_ranges": list(kwargs["create_delivery"]),
            }
        )

    monkeypatch.setattr(compiler, "_llm_call_page_async", delivered)
    with pytest.raises(RuntimeError, match="Interrupted"):
        await prepared.generate()
    resumed = prepare_page_creation(**options)
    assert resumed.missing == len(resumed.batches) - 1
    calls.clear()

    async def complete(model, messages, stage, **kwargs):
        calls.append(stage)
        assert options["fits"](messages)
        return json.dumps(
            {
                "description": "Document rules.",
                "type": "other",
                "content": "Supported tail.",
                "covered_ranges": list(kwargs["create_delivery"]),
            }
        )

    monkeypatch.setattr(compiler, "_llm_call_page_async", complete)
    missing = resumed.missing
    result = await resumed.generate()
    assert len(calls) == missing
    assert result["content"].count("Supported rule.") == 1
    assert cp.load(prepared.batches[0].key, prepared.batches[0].signature) is not None
    assert prepare_page_creation(**options).missing == 0


def test_create_rejects_coverage_hole_even_with_valid_individual_windows(kb_dir):
    from dataclasses import replace

    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.page_response import PageResponseError

    options, _, _ = creation_inputs(kb_dir)
    originals = options["originals"]
    w = originals.windows[0]
    options["originals"] = replace(originals, windows=({**w, "selection_offset": 2},))
    with pytest.raises(PageResponseError, match="coverage"):
        prepare_page_creation(**options)


def test_oversized_executable_excerpt_is_not_silently_split(kb_dir):
    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.page_response import PageResponseError

    options, _, _ = creation_inputs(kb_dir)
    source = "# Procedure\n\n```sh\n" + ("echo exact-original-value\n" * 400) + "```\n"
    (kb_dir / "wiki/sources/manual.md").write_text(source)
    from ultrakb.agent.source_windows import CompileSourceWindows

    reader = CompileSourceWindows(kb_dir / "wiki", "manual", kb_dir=kb_dir)
    options["windows"] = reader
    options["originals"] = reader.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(source)}"}}, long=True
    )
    with pytest.raises(PageResponseError, match="indivisible_excerpt"):
        prepare_page_creation(**options)


def test_create_coverage_acknowledgement_is_exact():
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError

    raw = json.dumps({"content": "Grounded statement.", "covered_ranges": ["first"]})
    with pytest.raises(PageResponseError, match="page_incomplete_coverage"):
        validate_page_delivery(raw, expected_coverage=("first", "tail"))


@pytest.mark.asyncio
async def test_creation_repair_is_persisted_and_never_reopened_on_resume(kb_dir, monkeypatch):
    from ultrakb.agent import compiler
    from ultrakb.agent.create_generation import prepare_page_creation

    options, _, cp = creation_inputs(kb_dir)
    prepared = prepare_page_creation(**options)
    calls = []

    async def delivered(model, messages, stage, **kw):
        assert options["fits"](messages)
        calls.append(stage)
        if len(calls) == 1:
            return json.dumps({"description": "Rules.", "content": "Supported rules."})
        match = next(
            m["content"]
            for m in messages
            if isinstance(m["content"], str) and '"required_covered_ranges"' in m["content"]
        )
        coverage = json.loads(
            match.split('"required_covered_ranges": ', 1)[1].split("]", 1)[0] + "]"
        )
        return json.dumps(
            {"description": "Rules.", "content": "Supported rules.", "covered_ranges": coverage}
        )

    monkeypatch.setattr(compiler, "_llm_call_async", delivered)
    await prepared.generate()
    first = prepared.batches[0]
    _, entry = cp._entry(first.key, first.signature)
    assert entry["state"] == "accepted" and entry["repair_used"] is True
    assert len(calls) == len(prepared.batches) + 1
    calls.clear()
    await prepare_page_creation(**options).generate()
    assert calls == []


def test_projection_preserves_captions_table_facts_and_partial_coverage():
    from ultrakb.agent.create_requests import original_message
    from ultrakb.agent.page_originals import PageOriginalContext

    w = {
        "content": "Original operation.",
        "images": [{"path": "image.png"}],
        "units": [
            {
                "page": 1,
                "printed_page_label": "iii",
                "text_coverage": "partial",
                "images": [{"path": "image.png", "caption": "Original image caption."}],
                "structure_coverage": "unconfirmed",
            }
        ],
        "native_tables": [{"headers": ["Mode", "Limit"], "rows": [["special", "7"]]}],
    }
    message = {"role": "user", "content": [{"type": "text", "text": json.dumps([w])}]}
    originals = PageOriginalContext(message, True, True, (w,))
    result = original_message(originals, [w])["content"][0]["text"]
    projected = json.loads(result.split("\n", 1)[0])[0]
    assert projected["native_tables"] == w["native_tables"]
    assert projected["units"] == w["units"]
    assert "images" not in projected
    assert "Pixels have not been interpreted" in result


def test_planner_and_sender_share_generation_merging_and_wire_shape():
    from ultrakb.agent.compile_capacity import CompileCapacity
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.execution_capacity import request_input
    from ultrakb.llm_execution import CompletionExecutor, ModelRequest, RoleBindings

    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            LlmCredentialBundle(api_key="private", base_url="https://example.invalid"),
            generation={"compile": {"max_tokens": 2000, "reasoning_effort": "low"}},
        )
    )
    sizer = CompileCapacity(executor, None)
    messages = [{"role": "user", "content": "Some original text."}]
    opts = {"response_format": {"type": "json_object"}, "max_completion_tokens": 4000}
    actual = executor._kwargs(
        "compile",
        ModelRequest(messages, operation="compile", stage="part", generation_options=opts),
    )
    assert sizer.payload(messages, opts) == request_input(actual)
    assert sizer.payload(messages, opts)["max_tokens"] == 4000
    assert sizer.payload(messages, opts)["reasoning_effort"] == "low"
    assert "private" not in json.dumps(sizer.payload(messages, opts))


def test_byte_capacity_display_keeps_the_original_policy_identity(tmp_path, monkeypatch):
    from copy import deepcopy

    from ultrakb import config
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
    from ultrakb.capacity_display import capacity_description
    from ultrakb.execution_capacity import capacity_policy

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
    values = {
        "model": CONNECTION_TEMPLATES[2].model,
        "model_capacity": {"max_input_bytes": 65536},
    }
    policy = capacity_policy(values, custom_endpoint=True)
    before = deepcopy(policy)
    display = capacity_description(policy)
    assert "65536 bytes" in display and "字节护栏" in display
    assert policy == before


def test_new_byte_guard_is_distinct_from_model_token_capacity():
    from ultrakb.execution_capacity import capacity_policy
    from ultrakb.model_tokenizer import TokenMeasure

    policy = capacity_policy(
        {"model": "private/model", "model_capacity": {"max_input_bytes": 65536}},
        custom_endpoint=True,
    )
    measure = TokenMeasure(policy)
    assert measure.unit == "utf8_byte"
    assert measure.count("中文") == 6
    assert measure.input_limit == 65536
    assert policy["context_window_tokens"] is None
    from ultrakb.model_tokenizer import ContextCapacityError

    with pytest.raises(ContextCapacityError, match="UTF-8 bytes"):
        measure.require_current("中" * 22000)


def test_capacity_mode_switch_requires_explicit_merge_patch_clears():
    from ultrakb.application.settings import _merge_config_patch
    from ultrakb.execution_capacity import capacity_policy

    current = {"model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"}}
    with pytest.raises(ValueError, match="byte guard"):
        _merge_config_patch(current, {"model_capacity": {"max_input_bytes": 65536}})
    merged = _merge_config_patch(
        current,
        {
            "model_capacity": {
                "max_input_tokens": None,
                "tokenizer_model": None,
                "max_input_bytes": 65536,
            }
        },
    )
    assert current["model_capacity"]["max_input_tokens"] == 65536
    assert (
        capacity_policy({"model": "private", **merged}, custom_endpoint=True)["input_limit"]
        == 65536
    )
