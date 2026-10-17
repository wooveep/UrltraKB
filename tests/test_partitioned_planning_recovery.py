"""Planning receipts survive interruption using the existing real checkpoint storage."""

import json

import pytest

from ultrakb.agent.compile_capacity import CompileCapacity
from ultrakb.agent.partition_planning import partitioned_plan
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.compile_checkpoints import CompileCheckpoints
from ultrakb.config import LlmCredentialBundle
from ultrakb.execution_capacity import capacity_policy, require_request_capacity
from ultrakb.llm_execution import CompletionExecutor, RoleBindings
from ultrakb.locks import atomic_write_text
from ultrakb.model_task import TaskJournal
from ultrakb.model_tokenizer import TokenMeasure

pytestmark = pytest.mark.integration


def test_partitioned_plan_resumes_only_missing_native_work(kb_dir):
    wiki = kb_dir / "wiki"
    atomic_write_text(wiki / "sources/manual.json", '[{"page":1,"content":"Original."}]')
    atomic_write_text(wiki / "summaries/manual.md", "---\nfull_text: sources/manual.json\n---\n")
    navigation = [
        {
            "node_id": f"node-{i}",
            "title": f"Section {i} " + "title " * 100,
            "start_index": 1,
            "end_index": 1,
        }
        for i in range(10)
    ]
    windows = CompileSourceWindows(wiki, "manual", navigation=navigation, kb_dir=kb_dir)
    executor = CompletionExecutor(RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle()))
    measure = TokenMeasure(
        capacity_policy(
            {
                "model": "deepseek/deepseek-flash",
                "model_capacity": {"max_input_bytes": 5000},
            },
            custom_endpoint=False,
        )
    )
    fits = CompileCapacity(executor, measure)
    task = TaskJournal(kb_dir, executor)
    checkpoints = CompileCheckpoints(kb_dir, task.record, {"document": "manual"}, {"protocol": 1})
    accepted = []
    failed = False

    def send(messages, stage):
        nonlocal failed
        require_request_capacity(measure, fits.payload(messages), operation="compile")
        task_input = json.loads(messages[-1]["content"].split("\n", 1)[1])
        if accepted and not failed:
            failed = True
            raise RuntimeError("Stopped after one accepted planning partition")
        ids = [item["node_id"] for item in task_input["work"]]
        accepted.extend(ids)
        return json.dumps(
            {
                "coverage": [
                    {"id": value, "candidates": [], "reason": "Navigation only."} for value in ids
                ],
                "candidates": [],
            }
        )

    def run():
        return partitioned_plan(
            system={"role": "system", "content": "Compilation rules."},
            windows=windows,
            fits=fits,
            send=send,
            entity_types=["other"],
            sanitize=lambda name: name,
            checkpoints=checkpoints,
        )

    with pytest.raises(RuntimeError, match="Stopped"):
        run()
    first = tuple(accepted)
    assert first and len(first) < 10
    result = run()
    assert accepted == [f"node-{i}" for i in range(10)]
    assert not result.concepts.create and not result.entities.create
    assert checkpoints.path.is_file()


def test_capacity_diagnostic_reports_wire_totals_without_provider_usage():
    config = {"model": "deepseek/deepseek-flash", "model_capacity": {"max_input_bytes": 10000}}
    executor = CompletionExecutor(RoleBindings(config["model"], LlmCredentialBundle()))
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    fits = CompileCapacity(executor, measure)
    messages = [
        {"role": "system", "content": "Required rules and schema."},
        {"role": "user", "content": "Generated navigation for document.\nNative catalog."},
        {"role": "assistant", "content": "Accepted generated overview."},
        {"role": "user", "content": "Repair only this planning work.\nCorrect native IDs."},
    ]
    report = fits.diagnostic(messages)
    assert report["input_units"] == measure.payload_tokens(fits.payload(messages))
    assert report["serialized_bytes"] == len(
        json.dumps(fits.payload(messages), ensure_ascii=False).encode()
    )
    assert report["provider_billing_tokens"] is False
    assert report["unit"] == "utf8_byte"
    assert all(
        report["contributions"][category]["local_units_removed"] > 0
        for category in ("rules_schema", "native_catalog", "overview", "correction")
    )
    assert "not additive" in report["contribution_method"]


def test_received_valid_partition_survives_checkpoint_disk_failure(kb_dir, monkeypatch):
    import ultrakb.compile_checkpoints as checkpoint_storage

    wiki = kb_dir / "wiki"
    atomic_write_text(wiki / "sources/manual.json", '[{"page":1,"content":"Original."}]')
    atomic_write_text(wiki / "summaries/manual.md", "---\nfull_text: sources/manual.json\n---\n")
    windows = CompileSourceWindows(
        wiki,
        "manual",
        kb_dir=kb_dir,
        navigation=[
            {"node_id": "native", "title": "Topic", "start_index": 1, "end_index": 1},
        ],
    )
    executor = CompletionExecutor(RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle()))
    task = TaskJournal(kb_dir, executor)
    checkpoints = CompileCheckpoints(kb_dir, task.record, {"document": "manual"}, {"protocol": 1})
    received = []

    def send(messages, stage):
        received.append(stage)
        return (
            '{"coverage":[{"id":"native","candidates":[],"reason":"Navigation only."}],'
            '"candidates":[]}'
        )

    def run():
        return partitioned_plan(
            system={"role": "system", "content": "Compile JSON."},
            windows=windows,
            fits=lambda messages: True,
            send=send,
            entity_types=["other"],
            sanitize=lambda name: name,
            checkpoints=checkpoints,
        )

    write = checkpoint_storage.atomic_write_json

    def disk_failure(path, value):
        if path.parent == checkpoints.directory and path.name.startswith("payload-"):
            raise OSError("Checkpoint disk failure after response was received")
        return write(path, value)

    with monkeypatch.context() as failure:
        failure.setattr(checkpoint_storage, "atomic_write_json", disk_failure)
        with pytest.raises(OSError, match="disk failure"):
            run()
    result = run()
    assert received == ["concepts-plan-partition"]
    assert not result.concepts.create


def test_synonymous_mixed_native_scopes_keep_every_selected_original(kb_dir):
    wiki = kb_dir / "wiki"
    atomic_write_text(
        wiki / "sources/manual.json",
        '[{"page":1,"content":"Original one."},{"page":2,"content":"Original two."}]',
    )
    atomic_write_text(wiki / "summaries/manual.md", "---\nfull_text: sources/manual.json\n---\n")
    navigation = [
        {
            "node_id": "root",
            "title": "Topic",
            "start_index": 1,
            "end_index": 1,
            "nodes": [
                {"node_id": "child", "title": "Topic alias", "start_index": 2, "end_index": 2}
            ],
        }
    ]
    windows = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir, navigation=navigation)

    def send(messages, stage):
        task_input = json.loads(messages[-1]["content"].split("\n", 1)[1])
        if stage == "concepts-plan-partition":
            return json.dumps(
                {
                    "coverage": [
                        {"id": "root", "candidates": ["one"], "reason": "Topic."},
                        {"id": "child", "candidates": ["two"], "reason": "Same topic."},
                    ],
                    "candidates": [
                        {
                            "id": "one",
                            "kind": "concepts",
                            "name": "topic",
                            "title": "Topic",
                            "brief": "First contribution.",
                            "reading_locations": {"node_ids": ["root"]},
                        },
                        {
                            "id": "two",
                            "kind": "concepts",
                            "name": "alias",
                            "title": "Alias",
                            "brief": "Second contribution.",
                            "reading_locations": {"pages": "2"},
                        },
                    ],
                }
            )
        return json.dumps(
            {
                "matches": [
                    {
                        "id": item["id"],
                        "relation": "same",
                        "action": "update",
                        "reason": "Same meaning.",
                    }
                    for item in task_input["catalog"]
                ]
            }
        )

    result = partitioned_plan(
        system={"role": "system", "content": "Compile JSON."},
        windows=windows,
        fits=lambda messages: True,
        send=send,
        entity_types=["other"],
        sanitize=lambda name: name,
    )
    assert len(result.concepts.create) == 1
    item = result.concepts.create[0]
    assert item["reading_locations"] == {"pages": "1,2"}
    assert "First contribution" in item["brief"] and "Second contribution" in item["brief"]
    assert {"topic", "alias"} <= set(item["aliases"])
