"""Lossless, versioned CTree state referring to a fixed original-turn prefix."""

import copy
import json

from ctree import CTree
from ctree.state import prefix_digest, validate_turns

CODEC = "ultrakb.ctree-checkpoint"
CODEC_VERSION = 1


def encode_checkpoint(snapshot: dict, session_id: str, policy_key: str) -> bytes:
    # Let the SDK validate all recovery fields, including frozen/current nodes.
    CTree.restore_state(snapshot)
    if snapshot["conversation_id"] != session_id or snapshot["analysis_roles"] != ["user"]:
        raise ValueError("Checkpoint belongs to another conversation or role policy")
    if not isinstance(policy_key, str) or not policy_key:
        raise ValueError("Checkpoint requires an explicit policy identity")
    state = {key: value for key, value in snapshot.items() if key != "turns"}
    return json.dumps(
        {
            "codec": CODEC,
            "version": CODEC_VERSION,
            "session_id": session_id,
            "policy_key": policy_key,
            "turn_count": snapshot["turn_count"],
            "prefix_digest": snapshot["turn_prefix_digest"],
            "state": state,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def restore_checkpoint(
    payload: bytes, turns: list[dict], session_id: str, policy_key: str, *, llm=None
) -> CTree:
    try:
        value = json.loads(payload)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "codec",
                "version",
                "session_id",
                "policy_key",
                "turn_count",
                "prefix_digest",
                "state",
            }
            or value["codec"] != CODEC
            or type(value["version"]) is not int
            or value["version"] != CODEC_VERSION
            or value["session_id"] != session_id
            or value["policy_key"] != policy_key
            or type(value["turn_count"]) is not int
            or value["turn_count"] != len(turns)
            or not isinstance(value["state"], dict)
            or "turns" in value["state"]
        ):
            raise ValueError("Invalid checkpoint encoding or prefix identity")
        validate_turns(turns)
        if value["prefix_digest"] != prefix_digest(turns):
            raise ValueError("Original turn identity, order or content changed")
        snapshot = copy.deepcopy(value["state"])
        if (
            snapshot["conversation_id"] != session_id
            or snapshot["analysis_roles"] != ["user"]
            or snapshot["turn_count"] != value["turn_count"]
            or snapshot["turn_prefix_digest"] != value["prefix_digest"]
        ):
            raise ValueError("Checkpoint metadata does not describe its SDK state")
        snapshot["turns"] = turns
        return CTree.restore_state(snapshot, llm=llm)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid retained conversation checkpoint") from error
