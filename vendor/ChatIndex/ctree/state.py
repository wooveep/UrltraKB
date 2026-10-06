"""Versioned checkpoint validation and content-prefix identity, with no I/O."""
import copy
import hashlib
import json

from .nodes import MessageNode, TopicNode, walk
from .prompts import PROMPT_VERSION


class RebuildRequired(ValueError):
    """The retained tree cannot safely represent this source/policy."""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def content_digests(turn):
    return {m["role"]: digest(m["content"]) for m in turn["messages"]}


def prefix_digest(turns):
    return digest([{"turn_id": t["turn_id"], "ordinal": t["ordinal"],
                    "content_digests": content_digests(t)} for t in turns])


def roles(value):
    if not isinstance(value, (tuple, list)) or not value or any(
        not isinstance(role, str) or role not in {"user", "assistant", "system"} for role in value
    ) or len(set(value)) != len(value):
        raise ValueError("Invalid role policy")
    return tuple(value)


def validate_turns(turns):
    if not isinstance(turns, list):
        raise ValueError("Invalid turns")
    identities = set()
    for ordinal, turn in enumerate(turns):
        if not isinstance(turn, dict) or turn.get("ordinal") != ordinal or type(turn.get("ordinal")) is not int:
            raise ValueError("Turns must cover an ordered complete prefix")
        identity = turn.get("turn_id")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ValueError("Turn identities must be nonempty and unique")
        identities.add(identity)
        messages = turn.get("messages")
        if not isinstance(messages, list) or not all(isinstance(m, dict) and isinstance(m.get("content"), str) for m in messages):
            raise ValueError("Invalid original messages")
        message_roles = [m.get("role") for m in messages]
        if message_roles not in (["user", "assistant"], ["system", "user", "assistant"]):
            raise ValueError("Snapshot requires explicit complete exchanges")


def frozen_ids(tree):
    active = {node.node_id for node in tree.get_ancestors(tree.current_node)}
    return [n.node_id for n in walk(tree.root) if isinstance(n, TopicNode) and n.node_id not in active]


def snapshot(tree):
    return copy.deepcopy({"schema": "ctree.snapshot", "schema_version": 1,
        "conversation_id": tree.conversation_id, "analysis_roles": list(tree.analysis_roles),
        "prompt_version": PROMPT_VERSION, "max_children": tree.max_children,
        "model": tree.model, "turns": tree.turns, "turn_count": len(tree.turns),
        "turn_prefix_digest": prefix_digest(tree.turns), "root": tree.root.to_dict(),
        "current_node_id": tree.current_node.node_id, "next_node_id": tree.next_node_id,
        "frozen_node_ids": frozen_ids(tree)})


def decode(data):
    data = copy.deepcopy(data)
    if data.get("schema") != "ctree.snapshot" or (type(data.get("schema_version")) is not int or data.get("schema_version") != 1):
        raise RebuildRequired("Unknown or legacy checkpoint: rebuild required")
    if data.get("prompt_version") != PROMPT_VERSION:
        raise RebuildRequired("Prompt policy changed: rebuild required")
    roles(data.get("analysis_roles"))
    turns = data.get("turns")
    validate_turns(turns)
    if type(data.get("turn_count")) is not int or data["turn_count"] != len(turns) or data.get("turn_prefix_digest") != prefix_digest(turns):
        raise ValueError("Checkpoint prefix identity does not match its source")
    seen, covered = {}, []
    def node(value, parent=None, ordinal=0):
        if not isinstance(value, dict):
            raise ValueError("Invalid node")
        identity = value.get("node_id")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("Invalid or duplicate node identity")
        if type(value.get("ordinal")) is not int or value["ordinal"] != ordinal:
            raise ValueError("Invalid explicit node order")
        start, end = value.get("start_turn"), value.get("end_turn")
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(turns):
            raise ValueError("Invalid half-open turn range")
        children = value.get("children")
        if not isinstance(children, list):
            raise ValueError("Missing ordered children")
        if value.get("kind") == "exchange":
            if end != start + 1 or children or turns[start]["turn_id"] != value.get("turn_id"):
                raise ValueError("Invalid exchange reference")
            messages = {m["role"]: m for m in turns[start]["messages"]}
            result = MessageNode(node_id=identity, ordinal=ordinal, parent=parent,
                message_index=start, turn_id=value["turn_id"], user_message=messages["user"],
                assistant_message=messages["assistant"], system_message=messages.get("system"))
            covered.append(start)
        elif value.get("kind") == "topic":
            if not all(isinstance(value.get(k), str) for k in ("title", "summary")):
                raise ValueError("Invalid topic text")
            result = TopicNode(node_id=identity, ordinal=ordinal, parent=parent,
                              topic_name=value["title"], summary=value["summary"], start_index=start, end_index=end)
        else:
            raise ValueError("Unknown node kind")
        seen[identity] = result
        result.children = [node(child, result, i) for i, child in enumerate(children)]
        result.sub_node_count = len(children)
        if isinstance(result, TopicNode):
            cursor = start
            for child in result.children:
                if child.start_index != cursor:
                    raise ValueError("Tree coverage has a gap or overlap")
                cursor = child.end_index
            if cursor != end:
                raise ValueError("Tree coverage incomplete")
        return result
    root = node(data.get("root"))
    if root.node_id != "root" or not isinstance(root, TopicNode) or (root.start_index, root.end_index) != (0, len(turns)) or covered != list(range(len(turns))):
        raise ValueError("Tree does not cover the declared prefix")
    current = seen.get(data.get("current_node_id"))
    if not isinstance(current, TopicNode) or current.end_index != len(turns):
        raise ValueError("Current topic must end at the committed prefix")
    if any(isinstance(child, TopicNode) for child in current.children):
        raise ValueError("Current topic must be the active leaf")
    next_id = data.get("next_node_id")
    if type(next_id) is not int or next_id < 1 or any(
        not identity.removeprefix("topic-").isdigit() or int(identity.removeprefix("topic-")) >= next_id
        for identity in seen if identity.startswith("topic-")
    ):
        raise ValueError("Invalid node sequence")
    return data, root, current
