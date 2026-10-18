"""Checkpoint encoding uses real SDK trees and authoritative persisted exchanges."""

import json

import pytest


class TopicResponses:
    """Fixed provider-boundary responses; the real SDK builds and validates nodes."""

    def __init__(self):
        self.requests = []

    def complete(self, messages, *, stage, prompt_version):
        self.requests.append(messages)
        assert "DISPLAY_ONLY" not in json.dumps(messages)
        return '{"title":"Workspace"}' if stage == "name" else '{"belongs_to_current":true}'


def test_real_sdk_checkpoint_restores_full_original_exchanges(tmp_path):
    from ctree import CTree

    from ultrakb.storage.database import create_database
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "new-kb"
    identity = create_database(root)
    store = SessionStore(root, identity)
    store.create("session-one", model="deepseek/deepseek-flash", language="en")
    provider = TopicResponses()
    tree = CTree(conversation_id="session-one", llm=provider)
    for ordinal in range(3):
        user = f"Preference {ordinal}: quiet indoor workspace."
        assistant = f"DISPLAY_ONLY_{ordinal}"
        store.append_exchange("session-one", f"turn-{ordinal}", user, assistant)
        tree.add_exchange(f"turn-{ordinal}", user, assistant)
        checkpoint = store.save_checkpoint("session-one", "policy-one", tree.snapshot_state())

    restored = SessionStore(root, identity).restore_checkpoint("session-one", "policy-one")
    assert restored.snapshot_state() == tree.snapshot_state()
    assert [
        message["content"] for message in restored.conversation if message["role"] == "assistant"
    ] == ["DISPLAY_ONLY_0", "DISPLAY_ONLY_1", "DISPLAY_ONLY_2"]
    assert store.save_checkpoint("session-one", "policy-one", tree.snapshot_state()) == checkpoint
    assert "DISPLAY_ONLY" not in json.dumps(provider.requests)


@pytest.mark.parametrize("damage", ["missing", "content", "role", "order", "session", "policy"])
def test_checkpoint_rejects_changed_original_prefix(tmp_path, damage):
    from ctree import CTree

    from ultrakb.storage.database import create_database
    from ultrakb.storage.session_codec import encode_checkpoint, restore_checkpoint
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "new-kb"
    store = SessionStore(root, create_database(root))
    store.create("session-one", model="gpt-4o-mini", language="en")
    tree = CTree(conversation_id="session-one", llm=TopicResponses())
    for ordinal in range(2):
        store.append_exchange("session-one", f"turn-{ordinal}", "Preference", "DISPLAY_ONLY")
        tree.add_exchange(f"turn-{ordinal}", "Preference", "DISPLAY_ONLY")
    encoded = encode_checkpoint(tree.snapshot_state(), "session-one", "policy-one")
    assert "turns" not in json.loads(encoded)["state"]
    originals = store.turns("session-one")
    session_id, policy_key = "session-one", "policy-one"
    if damage == "missing":
        originals.pop()
    elif damage == "content":
        originals[0]["messages"][0]["content"] = "tampered"
    elif damage == "role":
        originals[0]["messages"].reverse()
    elif damage == "order":
        originals.reverse()
    elif damage == "session":
        session_id = "other-session"
    else:
        policy_key = "other-policy"
    with pytest.raises(ValueError):
        restore_checkpoint(encoded, originals, session_id, policy_key)
