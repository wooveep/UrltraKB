"""Strict ctree.retrieval v1 interchange, independent of the ctree runtime."""
import copy
import hashlib
import json
import re

from contextdb.core.json_values import validate_json
from .protocol import BaseAdapter


class ChatIndexContractError(ValueError):
    """Unsupported or unverifiable conversation projection."""


def _require(condition, message):
    if not condition:
        raise ChatIndexContractError(message)


def _fields(value, expected):
    _require(set(value) == set(expected.split()), "Unexpected or missing fields in conversation format")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _roles(value):
    _require(isinstance(value, list) and bool(value), "Missing role policy")
    _require(all(isinstance(role, str) and role in {"user", "assistant", "system"} for role in value), "Invalid role policy")
    _require(len(set(value)) == len(value), "Duplicate policy role")
    return set(value)


def _turns(source):
    basis, turns = source.get("basis"), source.get("turns")
    _require(isinstance(basis, dict) and isinstance(turns, list), "Missing source prefix")
    _fields(basis, "turn_count turn_prefix_digest analysis_roles content_roles prompt_version")
    count = basis.get("turn_count")
    _require(type(count) is int and count == len(turns), "Invalid turn_count")
    analysis, content = _roles(basis.get("analysis_roles")), _roles(basis.get("content_roles"))
    _require(analysis <= content, "Topic analysis contains excluded roles; rebuild required")
    _require(basis.get("prompt_version") == "chat-topics-v1", "Unsupported prompt policy")
    seen, prefix = set(), []
    for ordinal, turn in enumerate(turns):
        _require(isinstance(turn, dict), "Invalid turn")
        _fields(turn, "turn_id ordinal messages content_digests")
        identity = turn.get("turn_id")
        _require(isinstance(identity, str) and bool(identity) and identity not in seen, "Missing or duplicate turn identity")
        seen.add(identity)
        _require(type(turn.get("ordinal")) is int and turn["ordinal"] == ordinal, "Turns must cover an ordered prefix")
        hashes, messages = turn.get("content_digests"), turn.get("messages")
        _require(isinstance(hashes, dict) and set(hashes) in ({"user", "assistant"}, {"system", "user", "assistant"}), "Incomplete source content digests")
        _require(all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes.values()), "Invalid source digest")
        _require(isinstance(messages, list) and all(isinstance(m, dict) and isinstance(m.get("content"), str) for m in messages), "Invalid complete messages")
        expected = [role for role in ("system", "user", "assistant") if role in content and role in hashes]
        _require([m.get("role") for m in messages] == expected, "Messages do not match the declared role policy")
        for message in messages:
            _fields(message, "role content")
            _require(_digest(message["content"]) == hashes[message["role"]], "Message content digest changed")
        prefix.append({"turn_id": identity, "ordinal": ordinal, "content_digests": hashes})
    _require(basis.get("turn_prefix_digest") == _digest(prefix), "Turn prefix digest changed")
    return basis, turns


class ChatIndexAdapter(BaseAdapter):
    def convert(self, source_json):
        _require(isinstance(source_json, dict), "Unsupported conversation format")
        _require(source_json.get("schema") == "ctree.retrieval" and type(source_json.get("schema_version")) is int and source_json["schema_version"] == 1,
                 "Unsupported conversation format; complete ctree.retrieval v1 required")
        _require(not {"topics", "tree", "conversation"}.intersection(source_json), "Mixed conversation formats are unsupported")
        _fields(source_json, "schema schema_version conversation_id basis root turns")
        validate_json(source_json)
        conversation_id = source_json.get("conversation_id")
        _require(isinstance(conversation_id, str) and bool(conversation_id), "Missing conversation_id")
        basis, turns = _turns(source_json)
        seen, covered, entities = set(), [], {}

        def project(node, ordinal, *, root=False):
            _require(isinstance(node, dict), "Invalid tree node")
            identity = node.get("node_id")
            _require(isinstance(identity, str) and bool(identity) and identity not in seen, "Missing or duplicate node identity")
            seen.add(identity)
            _require(type(node.get("ordinal")) is int and node["ordinal"] == ordinal, "Node order changed")
            start, end = node.get("start_turn"), node.get("end_turn")
            _require(type(start) is int and type(end) is int and 0 <= start <= end <= len(turns), "Invalid half-open turn range")
            _require(root or start < end, "Empty non-root topic")
            children, kind = node.get("children"), node.get("kind")
            _require(isinstance(children, list), "Missing ordered children")
            attrs = {"node_id": identity, "ordinal": ordinal, "kind": kind, "start_turn": start, "end_turn": end}
            _fields(node, "node_id ordinal kind start_turn end_turn children " + ("turn_id" if kind == "exchange" else "title summary"))
            if kind == "exchange":
                _require(not root and end == start + 1 and not children, "Exchange must reference exactly one turn")
                _require(node.get("turn_id") == turns[start]["turn_id"], "Missing or mismatched turn reference")
                attrs["turn_id"] = node["turn_id"]
                entities[identity] = {"type": "exchange", **attrs,
                    "messages": copy.deepcopy(turns[start]["messages"])}
                covered.append(start)
            elif kind == "topic":
                _require(all(isinstance(node.get(k), str) for k in ("title", "summary")), "Missing topic text")
                attrs.update(title=node["title"], summary=node["summary"])
                entities[identity] = {"type": "topic", **attrs}
            else:
                raise ChatIndexContractError("Unknown node kind")
            projected = [project(child, i) for i, child in enumerate(children)]
            if kind == "topic":
                cursor = start
                for child in projected:
                    _require(child["attrs"]["start_turn"] == cursor, "Topic coverage has a gap, overlap or changed order")
                    cursor = child["attrs"]["end_turn"]
                _require(cursor == end, "Incomplete topic coverage")
            if root:
                _require(identity == "root" and kind == "topic" and (start, end) == (0, len(turns)), "Root must cover the complete source prefix")
                entities[identity].update(type="conversation", conversation_id=conversation_id, basis=copy.deepcopy(basis))
                attrs["conversation_id"] = conversation_id
            return {"node_id": identity, "type": "object" if children else "leaf",
                    "attrs": attrs, "entity_id": identity, "children": projected}

        tree = project(source_json.get("root"), 0, root=True)
        _require(covered == list(range(len(turns))), "Tree must cover every source turn exactly once")
        return tree, entities
