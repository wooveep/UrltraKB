"""Real incremental CTree, lossless checkpoints, and isolated retrieval policy."""

import copy
import json

import pytest
from ctree import CTree
from ctree.state import RebuildRequired

pytest_plugins = ("test_vendor_sdk",)


class Recorder:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.new_topic = False
        self.invalid = 0

    def complete(self, messages, *, stage, prompt_version):
        self.calls.append((stage, messages, prompt_version))
        if self.fail:
            raise self.fail
        if self.invalid:
            self.invalid -= 1
            return "invalid JSON"
        payload = json.loads(messages[0]["content"].split("\n", 2)[2])
        stage = stage.removesuffix(".repair")
        if stage == "name":
            return '{"title":"User topic"}'
        if stage == "classify":
            if self.new_topic:
                return '{"belongs_to_current":false,"title":"New topic","parent_id":"root"}'
            return '{"belongs_to_current":true}'
        if stage == "split":
            n = payload["child_count"]
            return json.dumps(
                {
                    "groups": [
                        {"title": "First", "start": 0, "end": n // 2},
                        {"title": "Second", "start": n // 2, "end": n},
                    ]
                }
            )
        return '{"summary":"Only user facts"}'


def build(count=10, *, new_topic=False):
    recorder = Recorder()
    recorder.new_topic = new_topic
    tree = CTree(llm=recorder, max_children=2, conversation_id="session-fixture")
    for i in range(count):
        tree.add_exchange(
            f"t{i}",
            f"User {i}: " + "中文内容" * 80,
            "SECRET_WRONG_ASSISTANT_" + str(i),
            system="SECRET_SYSTEM",
        )
    return tree, recorder


@pytest.mark.parametrize("new_topic", [False, True])
def test_multilevel_splits_cover_every_complete_turn_and_filter_every_prompt(new_topic):
    tree, recorder = build(new_topic=new_topic)
    tree.refresh_summaries()
    assert {s for s, _, _ in recorder.calls} >= {
        "name",
        "classify",
        "split",
        "frozen_summary",
        "summary",
    }
    assert all("SECRET_" not in str(messages) for _, messages, _ in recorder.calls)
    export = tree.export_retrieval_tree()
    assert export["basis"]["turn_count"] == 10
    assert len(export["turns"][0]["messages"][0]["content"]) > 200
    assert "SECRET_" not in json.dumps(export)

    def leaves(node):
        if node["kind"] == "exchange":
            return [node["turn_id"]]
        return [turn for child in node["children"] for turn in leaves(child)]

    assert leaves(export["root"]) == [f"t{i}" for i in range(10)]
    assert export["root"]["start_turn"] == 0 and export["root"]["end_turn"] == 10


def test_stable_duplicate_is_noop_conflict_and_any_llm_failure_are_atomic():
    tree, recorder = build(1)
    before = tree.snapshot_state()
    count = len(recorder.calls)
    assert not tree.add_exchange(
        "t0",
        before["turns"][0]["messages"][1]["content"],
        "SECRET_WRONG_ASSISTANT_0",
        system="SECRET_SYSTEM",
    )
    assert len(recorder.calls) == count
    with pytest.raises(RebuildRequired):
        tree.add_exchange("t0", "changed", "assistant")
    recorder.fail = RuntimeError("transport failed")
    with pytest.raises(RuntimeError, match="transport failed"):
        tree.add_exchange("t1", "next", "assistant")
    assert len(recorder.calls) == count + 1
    assert tree.snapshot_state() == before


@pytest.mark.parametrize("new_topic", [False, True])
def test_snapshot_restore_export_are_pure_and_next_append_matches(monkeypatch, new_topic):
    tree, recorder = build(8, new_topic=new_topic)
    tree.refresh_summaries()
    snapshot = tree.snapshot_state()

    def forbidden(*a, **kw):
        pytest.fail("Pure serialization attempted network or file write")

    with monkeypatch.context() as pure:
        pure.setattr(recorder, "complete", forbidden)
        pure.setattr("builtins.open", forbidden)
        pure.setattr("pathlib.Path.write_text", forbidden)
        restored = CTree.restore_state(snapshot, llm=recorder)
        assert restored.snapshot_state() == snapshot
        assert restored.export_retrieval_tree() == tree.export_retrieval_tree()
    tree.add_exchange("next", "next user", "secret")
    restored.add_exchange("next", "next user", "secret")
    assert restored.snapshot_state() == tree.snapshot_state()


def test_only_format_failure_is_repaired_and_still_user_only():
    tree, recorder = build(1)
    recorder.invalid = 1
    tree.add_exchange("t1", "new", "SECRET_REPAIR")
    assert recorder.calls[-1][0] == "classify.repair"
    assert "SECRET_" not in str(recorder.calls)
    before = tree.snapshot_state()
    recorder.invalid = 2
    with pytest.raises(json.JSONDecodeError):
        tree.add_exchange("t2", "new", "SECRET_REPAIR")
    assert tree.snapshot_state() == before


def test_assistant_analyzed_topics_cannot_be_relabeled_user_only():
    tree = CTree(llm=Recorder(), analysis_roles=("user", "assistant"))
    tree.add_exchange("t", "question", "answer")
    with pytest.raises(RebuildRequired):
        tree.export_retrieval_tree(content_roles=("user",))


@pytest.mark.parametrize(
    "damage", ["digest", "forged_digest", "order", "duplicate", "range", "schema"]
)
def test_restore_rejects_unverifiable_checkpoint(damage):
    tree, _ = build(2)
    snapshot = copy.deepcopy(tree.snapshot_state())
    if damage in {"digest", "forged_digest"}:
        if damage == "forged_digest":
            from ctree.state import content_digests

            snapshot["turns"][0]["content_digests"] = content_digests(snapshot["turns"][0])
        snapshot["turns"][0]["messages"][0]["content"] = "changed"
    elif damage == "order":
        snapshot["turns"].reverse()
    elif damage == "duplicate":
        snapshot["root"]["children"][0]["node_id"] = "root"
    elif damage == "range":
        snapshot["root"]["end_turn"] = 1
    else:
        snapshot["schema_version"] = 2
    with pytest.raises(ValueError):
        CTree.restore_state(snapshot)


def test_real_runtime_adapter_uses_shared_budget_and_never_serializes_secrets(
    model_service, kb_dir
):
    from test_llm_execution import executor_for
    from test_vendor_sdk import response

    from openkb.conversation_index import create_conversation_index
    from openkb.llm_execution import ModelBudgetExceeded
    from openkb.llm_usage import read_requests
    from openkb.llm_usage_execution import import_usage_execution

    model_service.replies.append(
        (200, response({"role": "assistant", "content": '{"title":"User topic"}'}))
    )
    with import_usage_execution(kb_dir, "conversation_index"):
        runtime = executor_for(model_service, max_calls=1)
        tree = create_conversation_index("session-fixture", executor=runtime)
        tree.add_exchange("first", "a user fact", "SECRET")
        before = tree.snapshot_state()
        assert before["model"] == runtime.bindings.model("conversation_index")
        assert CTree.restore_state(before).model == before["model"]
        with pytest.raises(ModelBudgetExceeded):
            tree.add_exchange("second", "new fact", "SECRET")
        assert tree.snapshot_state() == before
    assert len(model_service.requests) == len(read_requests(kb_dir)) == 1
    assert read_requests(kb_dir)[0].operation == "conversation_index"
    assert "api_key" not in json.dumps(before) and model_service.url not in json.dumps(before)


def test_failure_during_split_rolls_back_appended_turn_and_node_ids():
    tree, recorder = build(2)
    before = tree.snapshot_state()
    original = recorder.complete

    def fail_split(messages, *, stage, prompt_version):
        if stage == "split":
            raise RuntimeError("split transport failed")
        return original(messages, stage=stage, prompt_version=prompt_version)

    recorder.complete = fail_split
    with pytest.raises(RuntimeError, match="split transport failed"):
        tree.add_exchange("t2", "new", "assistant")
    assert tree.snapshot_state() == before


def test_import_does_not_load_a_decoy_dotenv(tmp_path):
    import subprocess
    import sys

    (tmp_path / ".env").write_text("CHATINDEX_DECOY=must-not-load\n")
    script = (
        'import os, ctree; assert "CHATINDEX_DECOY" not in os.environ; '
        "ctree.CTree().snapshot_state()"
    )
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, check=True)
