"""Real incremental CTree, lossless checkpoints, and isolated retrieval policy."""

import copy
import json

import pytest
from ctree import CTree
from ctree.state import RebuildRequired

pytest_plugins = ("test_vendor_sdk",)


class Recorder:
    """Record inputs while delegating every generation to the real provider."""

    def __init__(self, service):
        self.service = service
        self.calls = []

    def complete(self, messages, *, stage, prompt_version):
        self.calls.append((stage, messages, prompt_version))
        delivered = self.service.owner.completion(
            model="deepseek/deepseek-flash",
            messages=messages,
            api_key=self.service.credentials.api_key,
            api_base=self.service.url,
            max_tokens=256,
            response_format={"type": "json_object"},
            temperature=0,
        )
        return delivered.choices[0].message.content


def build(service, count=6, *, new_topic=False):
    recorder = Recorder(service)
    tree = CTree(llm=recorder, max_children=2, conversation_id="session-fixture")
    for i in range(count):
        user = (
            f"Recipe {i}: bake bread with {i + 1} cups of flour."
            if new_topic and i % 2
            else f"Workspace preference {i}: keep the room quiet."
        )
        tree.add_exchange(f"t{i}", user, "SECRET_WRONG_ASSISTANT_" + str(i), system="SECRET_SYSTEM")
    return tree, recorder


@pytest.mark.parametrize("new_topic", [False, True])
def test_multilevel_splits_cover_every_complete_turn_and_filter_every_prompt(
    model_service, new_topic
):
    tree, recorder = build(model_service, new_topic=new_topic)
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
    assert export["basis"]["turn_count"] == 6
    assert (
        export["turns"][0]["messages"][0]["content"]
        == "Workspace preference 0: keep the room quiet."
    )
    assert "SECRET_" not in json.dumps(export)

    def leaves(node):
        if node["kind"] == "exchange":
            return [node["turn_id"]]
        return [turn for child in node["children"] for turn in leaves(child)]

    assert leaves(export["root"]) == [f"t{i}" for i in range(6)]
    assert export["root"]["start_turn"] == 0 and export["root"]["end_turn"] == 6


def test_stable_duplicate_is_noop_conflict_and_any_llm_failure_are_atomic(
    model_service, monkeypatch
):
    import httpx
    import litellm

    tree, recorder = build(model_service, 1)
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
    send = httpx.Client._send_single_request

    def disconnect(client, outgoing):
        if outgoing.url.host == "127.0.0.1":
            raise httpx.ConnectError("Local connection interrupted", request=outgoing)
        return send(client, outgoing)

    monkeypatch.setattr(httpx.Client, "_send_single_request", disconnect)
    with pytest.raises((litellm.APIConnectionError, litellm.InternalServerError)):
        tree.add_exchange("t1", "next", "assistant")
    assert len(recorder.calls) == count + 1
    assert tree.snapshot_state() == before


@pytest.mark.parametrize("new_topic", [False, True])
def test_snapshot_restore_export_are_pure_and_next_append_matches(
    model_service, monkeypatch, new_topic
):
    tree, recorder = build(model_service, 6, new_topic=new_topic)
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
    from ctree.builder import FormatError, parse_stage_response
    from ctree.prompts import render

    with pytest.raises(json.JSONDecodeError):
        parse_stage_response("invalid JSON", lambda value: value)
    with pytest.raises(FormatError):
        parse_stage_response('{"title":""}', lambda value: _required_title(value))
    turn = {
        "turn_id": "t1",
        "ordinal": 1,
        "messages": [
            {"role": "user", "content": "Prefer quiet indoor work."},
            {"role": "assistant", "content": "SECRET_REPAIR"},
        ],
    }
    from ctree.prompts import allowed_turns

    payload = {"new_exchange": allowed_turns([turn], ("user",))[0]}
    repair = render("classify", payload, ("user",), repair=True)
    assert "previous response had an invalid format" in str(repair)
    assert "SECRET_" not in str(repair)
    assert parse_stage_response('{"belongs_to_current":true}', lambda value: value) == {
        "belongs_to_current": True
    }


def _required_title(value):
    from ctree.builder import _text

    return _text(value, "title")


def test_assistant_analyzed_topics_cannot_be_relabeled_user_only(model_service):
    tree = CTree(llm=Recorder(model_service), analysis_roles=("user", "assistant"))
    tree.add_exchange("t", "question", "answer")
    with pytest.raises(RebuildRequired):
        tree.export_retrieval_tree(content_roles=("user",))


@pytest.mark.parametrize(
    "damage", ["digest", "forged_digest", "order", "duplicate", "range", "schema"]
)
def test_restore_rejects_unverifiable_checkpoint(model_service, damage):
    tree, _ = build(model_service, 2)
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
    from native_usage_fixtures import native_usage_execution, read_requests
    from test_llm_execution import executor_for

    from ultrakb.conversation_index import create_conversation_index
    from ultrakb.llm_execution import ModelBudgetExceeded

    with native_usage_execution(kb_dir, "conversation_index"):
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


def test_failure_during_split_rolls_back_appended_turn_and_node_ids(model_service, monkeypatch):
    from ctree import builder

    tree, recorder = build(model_service, 2)
    before = tree.snapshot_state()

    def reject_partition(*args, **kwargs):
        raise ValueError("Invalid local partition")

    monkeypatch.setattr(builder, "split", reject_partition)
    with pytest.raises(ValueError, match="Invalid local partition"):
        tree.add_exchange("t2", "Workspace preference 2: keep the room quiet.", "assistant")
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
