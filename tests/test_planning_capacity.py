"""Planning admission preserves native locators and never supplies fake model success."""

import json

import pytest

from ultrakb.agent.compile_capacity import CompileCapacity
from ultrakb.config import LlmCredentialBundle
from ultrakb.execution_capacity import capacity_policy, require_request_capacity
from ultrakb.llm_execution import CompletionExecutor, RoleBindings
from ultrakb.locks import atomic_write_text
from ultrakb.model_tokenizer import ContextCapacityError, TokenMeasure


def planning_request():
    from ultrakb.agent.compile_messages import planning_messages
    from ultrakb.agent.planning_capacity import navigation_messages

    full, compact = navigation_messages(
        "manual",
        "doc",
        [
            {
                "node_id": "parent",
                "title": "Original title",
                "start_index": 1,
                "end_index": 1,
                "summary": "Generated planning aid. " * 200,
                "nodes": [
                    {"node_id": "tail", "title": "Exception", "start_index": 2, "end_index": 2}
                ],
            }
        ],
    )
    messages = planning_messages(
        {"role": "system", "content": "Compilation rules."},
        full,
        "Retained overview.",
        concept_briefs="Known concepts.",
        entity_briefs="Known entities.",
        long=True,
    )
    return messages, compact


def capacity(limit):
    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            LlmCredentialBundle(),
            generation={"compile": {"max_tokens": 512}},
        )
    )
    measure = TokenMeasure(
        capacity_policy(
            {"model": "deepseek/deepseek-flash", "model_capacity": {"max_input_bytes": limit}},
            custom_endpoint=False,
        )
    )
    return CompileCapacity(executor, measure)


@pytest.mark.unit
def test_fitting_planning_messages_keep_historical_checkpoint_bytes():
    from ultrakb.agent.planning_capacity import fit_planning_messages
    from ultrakb.compile_checkpoints import digest

    messages, compact = planning_request()
    before = digest(messages)
    result = fit_planning_messages(messages, capacity(100000), compact_document_message=compact)
    assert result is messages and digest(result) == before
    assert "Generated planning aid." in str(result)


@pytest.mark.unit
def test_correction_rechecks_full_request_without_dropping_native_scope():
    from ultrakb.agent.planning_capacity import fit_planning_messages

    messages, compact = planning_request()
    sizing = capacity(100000)
    sizing = capacity(sizing.measure.payload_tokens(sizing.payload(messages)))
    assert fit_planning_messages(messages, sizing, compact_document_message=compact) is messages
    correction = {"role": "user", "content": "Correct the native reading scope. " * 20}
    result = fit_planning_messages(
        [*messages, correction], sizing, compact_document_message=compact
    )
    assert result[0] == messages[0] and result[2:] == [*messages[2:], correction]
    nodes = json.loads(result[1]["content"].split("\n", 1)[1])
    assert [n["node_id"] for n in nodes] == ["parent", "tail"]
    assert nodes[-1]["parent_node_id"] == "parent"
    assert "navigation_summary" not in str(result)
    require_request_capacity(sizing.measure, sizing.payload(result), operation="compile")
    assert sizing.payload(result)["max_tokens"] == 512
    assert "Generated planning aid." in str(messages)


@pytest.mark.unit
def test_irreducible_planning_catalog_is_retained_when_capacity_is_insufficient():
    from ultrakb.agent.planning_capacity import fit_planning_messages
    from ultrakb.compile_checkpoints import digest

    messages, compact = planning_request()
    before = digest(messages)
    with pytest.raises(ContextCapacityError, match="All native locators"):
        fit_planning_messages(messages, capacity(100), compact_document_message=compact)
    assert digest(messages) == before


@pytest.mark.asyncio
@pytest.mark.integration
async def test_retained_summary_reaches_planner_with_every_native_locator(kb_dir, monkeypatch):
    from ultrakb.agent import compiler, document_summary
    from ultrakb.source_navigation import planning_navigation

    wiki = kb_dir / "wiki"
    source = wiki / "sources/manual.json"
    atomic_write_text(
        source,
        json.dumps(
            [
                {"page": 1, "unit_kind": "page", "content": "Original prerequisite."},
                {"page": 2, "unit_kind": "page", "content": "Original operation."},
            ]
        ),
    )
    original = source.read_bytes()
    summary = wiki / "summaries/manual.md"
    atomic_write_text(summary, "---\nfull_text: sources/manual.json\n---\nRetained summary.\n")
    navigation = [
        {
            "node_id": "parent",
            "title": "Procedure",
            "start_index": 1,
            "end_index": 1,
            "summary": "Generated planning aid. " * 3000,
            "nodes": [
                {
                    "node_id": "tail",
                    "title": "Final operation",
                    "start_index": 2,
                    "end_index": 2,
                    "summary": "Generated tail locator. " * 3000,
                }
            ],
        }
    ]
    config = {
        "model": "deepseek/deepseek-flash",
        "language": "en",
        "model_capacity": {"max_input_bytes": 24000},
    }
    executor = CompletionExecutor(RoleBindings(config["model"], LlmCredentialBundle()))
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    sizing = CompileCapacity(executor, measure)
    summaries_loaded = []

    async def retained_summary(*args, **kwargs):
        # This test stops before receiving a planner response. Only the local
        # handoff from retained summary to request admission is exercised.
        summaries_loaded.append(True)
        return document_summary.SummaryDraft(
            "# Retained summary\n", "Accepted batch description.", "Accepted batch description.", ()
        )

    class PlannerInputCaptured(Exception):
        pass

    def capture(model, messages, stage, **kwargs):
        assert stage == "concepts-plan"
        payload = sizing.payload(messages)
        require_request_capacity(measure, payload, operation="compile")
        nodes = json.loads(messages[1]["content"].split("\n", 1)[1])
        expected = [
            {k: v for k, v in n.items() if k != "navigation_summary"}
            for n in planning_navigation(navigation)
        ]
        assert nodes == expected
        assert nodes[-1]["parent_node_id"] == "parent"
        assert "Accepted batch description." in str(messages)
        raise PlannerInputCaptured

    monkeypatch.setattr(document_summary, "build_document_summary", retained_summary)
    monkeypatch.setattr(compiler, "_llm_call", capture)
    monkeypatch.setattr("ultrakb.config.resolve_effective_config", lambda *args: (config, {}))
    with executor.activate(), pytest.raises(PlannerInputCaptured):
        await compiler.compile_long_doc(
            "manual", summary, "document-id", kb_dir, config["model"], navigation=navigation
        )
    assert summaries_loaded == [True]
    assert executor.calls == 0
    assert source.read_bytes() == original
