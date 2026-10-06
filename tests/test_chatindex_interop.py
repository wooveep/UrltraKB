"""Real ChatIndex exports through ConDB; no model calls during conversion/storage."""

import copy
import json

import pytest
from contextdb import ConDB, ContextTree, TreeDB
from contextdb.adapter import ChatIndexAdapter
from ctree import CTree
from test_chatindex import Recorder


def conversation(*, nested=False, count=14):
    llm = Recorder()
    llm.new_topic = nested
    tree = CTree(llm=llm, max_children=3 if nested else 20, conversation_id="interop-session")
    for ordinal in range(count):
        tree.add_exchange(
            f"turn-{ordinal}", f"用户原文 {ordinal} " + "🙂中文" * 100, "SECRET_ASSISTANT"
        )
        if ordinal == 6:
            tree = CTree.restore_state(tree.snapshot_state(), llm=llm)
    tree.refresh_summaries()
    return tree, llm


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("api", ["condb", "context_tree"])
def test_real_export_roundtrip_keeps_full_user_messages_and_order(
    tmp_path, monkeypatch, nested, api
):
    tree, llm = conversation(nested=nested)

    def forbidden(*a, **kw):
        pytest.fail("Serialization or storage called a model")

    monkeypatch.setattr(llm, "complete", forbidden)
    monkeypatch.setattr("litellm.completion", forbidden)
    exported = tree.export_retrieval_tree()
    with TreeDB(str(tmp_path / "context.sqlite")) as db:
        client = ConDB(storage=db) if api == "condb" else ContextTree(storage=db)
        identity = (
            client.store(exported, format="chatindex")
            if api == "condb"
            else client.index_chatindex(exported)
        )
        rows = db.get_subtree(identity, db.get_root_id(identity), with_entities=True)
        exchanges = [r for r in rows if r["entity"]["payload"]["type"] == "exchange"]
        assert len(exchanges) == 14
        for ordinal, row in enumerate(exchanges):
            payload = row["entity"]["payload"]
            assert payload["turn_id"] == f"turn-{ordinal}"
            assert payload["messages"] == exported["turns"][ordinal]["messages"]
            assert len(payload["messages"][0]["content"]) > 200
            assert row["node_id"] == payload["node_id"]
            assert (payload["start_turn"], payload["end_turn"]) == (ordinal, ordinal + 1)
        assert rows[0]["entity"]["payload"]["basis"] == exported["basis"]
        assert "SECRET_ASSISTANT" not in json.dumps(rows)


@pytest.mark.parametrize(
    "damage",
    [
        "schema",
        "version",
        "bool_version",
        "mixed",
        "conversation",
        "prefix",
        "count",
        "duplicate_turn",
        "duplicate_node",
        "reference",
        "ordinal",
        "gap",
        "overlap",
        "content",
        "message_role",
        "assistant_analysis",
        "missing_content",
        "unknown_kind",
    ],
)
def test_invalid_exports_fail_before_storage_without_rewriting_source(
    tmp_path, monkeypatch, damage
):
    tree, _ = conversation(count=2)
    value = tree.export_retrieval_tree()
    leaf = value["root"]["children"][0]["children"][0]
    if damage == "schema":
        value["schema"] = "ctree.snapshot"
    elif damage == "version":
        value["schema_version"] = 2
    elif damage == "bool_version":
        value["schema_version"] = True
    elif damage == "mixed":
        value["topics"] = []
    elif damage == "conversation":
        value["conversation_id"] = ""
    elif damage == "prefix":
        value["basis"]["turn_prefix_digest"] = "0" * 64
    elif damage == "count":
        value["basis"]["turn_count"] = 3
    elif damage == "duplicate_turn":
        value["turns"][1]["turn_id"] = "turn-0"
    elif damage == "duplicate_node":
        leaf["node_id"] = "root"
    elif damage == "reference":
        leaf["turn_id"] = "missing"
    elif damage == "ordinal":
        leaf["ordinal"] = 9
    elif damage == "gap":
        value["root"]["children"][0]["children"].pop()
    elif damage == "overlap":
        value["root"]["children"][0]["children"][1]["start_turn"] = 0
    elif damage == "content":
        value["turns"][0]["messages"][0]["content"] = "truncated"
    elif damage == "message_role":
        value["turns"][0]["messages"][0]["role"] = "assistant"
    elif damage == "assistant_analysis":
        value["basis"]["analysis_roles"] = ["user", "assistant"]
    elif damage == "missing_content":
        value["turns"][0]["messages"] = []
    else:
        leaf["kind"] = "unknown"
    before = copy.deepcopy(value)
    monkeypatch.setattr("litellm.completion", lambda *a, **kw: pytest.fail("Model called"))
    with TreeDB(str(tmp_path / "context.sqlite")) as db:
        with pytest.raises(ValueError):
            ConDB(storage=db).store(value, format="chatindex")
        assert db.conn.execute("SELECT count(*) FROM trees").fetchone()[0] == 0
        assert db.conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 0
    assert value == before


@pytest.mark.parametrize(
    "value",
    [
        {"conversation_id": "old", "topics": []},
        {"tree": {"children": [{"user_message": "x" * 200}]}},
        {"tree": {}, "conversation": [{"role": "user", "content": "full"}]},
        {},
    ],
)
def test_legacy_and_preview_formats_fail_explicitly(value):
    with pytest.raises(ValueError, match="Unsupported conversation format"):
        ChatIndexAdapter().convert(value)


def test_empty_current_export_and_public_import_remain_supported(tmp_path):
    from contextdb.adapter.base import ChatIndexAdapter as CompatibilityImport
    from contextdb.adapter.chatindex import ChatIndexAdapter as CanonicalImport

    assert CompatibilityImport is CanonicalImport
    exported = CTree().export_retrieval_tree()
    with ConDB(str(tmp_path / "context.sqlite")) as db:
        tree_id = db.store(exported, format="chatindex")
        root = db.storage.get_root_id(tree_id)
        assert db.get_content(tree_id, root)["basis"]["turn_count"] == 0
        assert db.storage.get_children(tree_id, root) == []


@pytest.mark.parametrize("where", ["topic", "exchange", "message", "turn"])
def test_nested_legacy_or_undeclared_content_is_rejected(where):
    tree, _ = conversation(count=1)
    value = tree.export_retrieval_tree()
    topic = value["root"]["children"][0]
    if where == "topic":
        topic["subtopics"] = [{"messages": ["lost legacy content"]}]
    elif where == "exchange":
        topic["children"][0]["user_message"] = "old preview"
    elif where == "message":
        value["turns"][0]["messages"][0]["assistant"] = "hidden assistant content"
    else:
        value["turns"][0]["conversation"] = ["old content"]
    with pytest.raises(ValueError, match="fields|format"):
        ChatIndexAdapter().convert(value)
