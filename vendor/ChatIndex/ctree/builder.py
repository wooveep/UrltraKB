"""Incremental chronological topic assignment and bounded consecutive splits."""
import json

from .nodes import MessageNode, TopicNode
from .prompts import PROMPT_VERSION, allowed_turns, render
from .state import digest


class FormatError(ValueError):
    """An algorithm response does not satisfy its explicit format."""


def _text(value, key):
    if not isinstance(value, dict) or not isinstance(value.get(key), str) or not value[key].strip():
        raise FormatError(f"Expected nonempty {key}")
    return value[key].strip()


def ask(tree, stage, payload, validate):
    if tree.llm is None:
        raise ValueError("Appending or refreshing requires an injected ChatLLM")
    for repair in (False, True):
        # Deliberately outside the format handler: transport/control exceptions
        # are never converted into a repair request or a fallback topic.
        raw = tree.llm.complete(render(stage, payload, tree.analysis_roles, repair),
                                stage=stage + (".repair" if repair else ""),
                                prompt_version=PROMPT_VERSION)
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
            return validate(value)
        except (json.JSONDecodeError, FormatError):
            if repair:
                raise
    raise AssertionError("Unreachable")


def new_topic(tree, title, children=()):
    node = TopicNode(node_id=f"topic-{tree.next_node_id:08d}", topic_name=title, children=list(children))
    tree.next_node_id += 1
    bounds(node)
    return node


def bounds(node):
    for ordinal, child in enumerate(node.children):
        child.parent, child.ordinal = node, ordinal
    node.sub_node_count = len(node.children)
    if node.children:
        node.start_index, node.end_index = node.children[0].start_index, node.children[-1].end_index
    node.summary = ""


def _classification(tree, turn):
    ancestors = tree.get_ancestors(tree.current_node)
    payload = {"ancestors": [{"node_id": n.node_id, "title": n.topic_name, "summary": n.summary} for n in ancestors],
               "current_topic": tree.current_node.node_id,
               "recent": allowed_turns(tree.turns[max(0, len(tree.turns)-3):], tree.analysis_roles),
               "new_exchange": allowed_turns([turn], tree.analysis_roles)[0]}
    def validate(value):
        if not isinstance(value, dict) or type(value.get("belongs_to_current")) is not bool:
            raise FormatError("Expected belongs_to_current boolean")
        if value["belongs_to_current"]:
            return tree.current_node
        title = _text(value, "title")
        parent = next((n for n in ancestors if n.node_id == value.get("parent_id")), None)
        if parent is None:
            raise FormatError("Unknown parent identity")
        return parent, title
    return ask(tree, "classify", payload, validate)


def split(tree, node):
    children = node.children
    payload = {"title": node.topic_name, "child_count": len(children),
               "children": [{"ordinal": i, "title": getattr(c, "topic_name", ""),
                             "turns": allowed_turns(tree.turns[c.start_index:c.end_index], tree.analysis_roles)}
                            for i, c in enumerate(children)]}
    def validate(value):
        groups = value.get("groups") if isinstance(value, dict) else None
        if not isinstance(groups, list) or len(groups) != 2:
            raise FormatError("Expected two consecutive groups")
        cursor = 0
        for group in groups:
            _text(group, "title")
            start, end = group.get("start"), group.get("end")
            if type(start) is not int or type(end) is not int or start != cursor or not start < end <= len(children):
                raise FormatError("Invalid consecutive child partition")
            cursor = end
        if cursor != len(children):
            raise FormatError("Incomplete partition")
        return groups
    groups = ask(tree, "split", payload, validate)
    parts = [new_topic(tree, group["title"], children[group["start"]:group["end"]]) for group in groups]
    # A leaf expands vertically. Internal non-root overflow splits into siblings;
    # the root retains its fixed identity and gains a new grouping level.
    if node is tree.root or all(isinstance(c, MessageNode) for c in children):
        node.children = parts
        bounds(node)
    else:
        parent = node.parent
        node.children, node.topic_name = parts[0].children, parts[0].topic_name
        bounds(node)
        parent.children.insert(node.ordinal + 1, parts[1])
        bounds(parent)
        if len(parent.children) > tree.max_children:
            split(tree, parent)


def append(tree, turn):
    if not tree.turns:
        title = ask(tree, "name", {"turns": allowed_turns([turn], tree.analysis_roles)}, lambda v: _text(v, "title"))
        target = new_topic(tree, title)
        tree.root.children.append(target)
        target.parent = tree.root
    else:
        choice = _classification(tree, turn)
        if isinstance(choice, tuple):
            parent, title = choice
            if parent.children and isinstance(parent.children[0], MessageNode):
                parent.children = [new_topic(tree, parent.topic_name, parent.children)]
                bounds(parent)
            target = new_topic(tree, title)
            parent.children.append(target)
            target.parent = parent
        else:
            target = choice
    tree.turns.append(turn)
    messages = {m["role"]: m for m in turn["messages"]}
    target.children.append(MessageNode(node_id="exchange-" + digest(turn["turn_id"]),
        turn_id=turn["turn_id"], message_index=turn["ordinal"], user_message=messages["user"],
        assistant_message=messages["assistant"], system_message=messages.get("system")))
    node = target
    while node:
        bounds(node)
        if len(node.children) > tree.max_children:
            split(tree, node)
        node = node.parent
    tree.current_node = tree.root
    while tree.current_node.children and isinstance(tree.current_node.children[-1], TopicNode):
        tree.current_node = tree.current_node.children[-1]


def summarize(tree):
    from .nodes import walk
    from .state import frozen_ids
    frozen = set(frozen_ids(tree))
    for node in walk(tree.root):
        if not isinstance(node, TopicNode) or node is tree.root or node.summary:
            continue
        payload = {"title": node.topic_name,
                   "turns": allowed_turns(tree.turns[node.start_index:node.end_index], tree.analysis_roles)}
        node.summary = ask(tree, "frozen_summary" if node.node_id in frozen else "summary",
                           payload, lambda v: _text(v, "summary"))
