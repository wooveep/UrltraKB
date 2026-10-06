"""Transactional incremental conversation tree with independent pure exports."""
import copy
import json
from pathlib import Path

from . import builder
from .export import retrieval
from .nodes import MessageNode, Node, TopicNode
from .state import RebuildRequired, decode, frozen_ids, roles, snapshot, validate_turns


class CTree:
    def __init__(self, max_children=5, api_key=None, model="gpt-4o-mini", auto_save_path=None,
                 *, llm=None, analysis_roles=("user",), conversation_id="conversation"):
        if type(max_children) is not int or max_children < 2:
            raise ValueError("max_children must be an integer >= 2")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("conversation_id must be nonempty")
        if not isinstance(model, str) or not model:
            raise ValueError("model must be nonempty")
        if auto_save_path is not None:
            raise ValueError("Implicit saving is unsupported; explicitly save a committed snapshot")
        if api_key is not None:
            if llm is not None:
                raise ValueError("Choose injected LLM or explicit provider configuration")
            from .llm import create_client
            llm = create_client(model=model, api_key=api_key)
        self.llm, self.model = llm, model
        self.analysis_roles = roles(analysis_roles)
        self.conversation_id, self.max_children = conversation_id, max_children
        self.turns = []
        self.root = TopicNode(node_id="root", topic_name="Conversation")
        self.current_node, self.next_node_id = self.root, 1

    @property
    def conversation(self):
        return copy.deepcopy([m for turn in self.turns for m in turn["messages"]])

    def get_ancestors(self, node, include_self=True, exclude_root=False):
        result = []
        node = node if include_self else node.parent
        while node:
            if node is not self.root or not exclude_root:
                result.append(node)
            node = node.parent
        return list(reversed(result))

    def add_exchange(self, turn_id, user, assistant, *, system=None):
        messages = ([{"role": "system", "content": system}] if system is not None else [])
        messages += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
        existing = next((t for t in self.turns if t["turn_id"] == turn_id), None)
        if existing:
            if existing["messages"] == messages:
                return False
            raise RebuildRequired("Turn identity has changed content; rebuild required")
        turn = {"turn_id": turn_id, "ordinal": len(self.turns), "messages": messages}
        validate_turns([*self.turns, turn])
        candidate = self.restore_state(self.snapshot_state(), llm=self.llm)
        builder.append(candidate, turn)
        # Verify complete coverage before publishing the candidate.
        candidate = self.restore_state(candidate.snapshot_state(), llm=self.llm)
        self.__dict__.update(candidate.__dict__)
        return True

    def add(self, messages, *, turn_id=None):
        if not isinstance(messages, list):
            raise ValueError("Expected explicit exchange messages")
        by_role = {m.get("role"): m.get("content") for m in messages if isinstance(m, dict)}
        if len(by_role) != len(messages) or set(by_role) not in ({"user", "assistant"}, {"system", "user", "assistant"}):
            raise ValueError("Expected exactly one user and assistant, with optional system")
        if turn_id is None:
            raise ValueError("Compatibility add requires a stable turn_id")
        return self.add_exchange(turn_id, by_role["user"], by_role["assistant"], system=by_role.get("system"))

    def refresh_summaries(self):
        candidate = self.restore_state(self.snapshot_state(), llm=self.llm)
        builder.summarize(candidate)
        self.__dict__.update(candidate.__dict__)

    generate_summaries = refresh_summaries

    def snapshot_state(self):
        return snapshot(self)

    @classmethod
    def restore_state(cls, data, *, llm=None):
        data, root, current = decode(data)
        tree = cls(llm=llm, max_children=data["max_children"], model=data["model"],
                   analysis_roles=data["analysis_roles"], conversation_id=data["conversation_id"])
        tree.turns, tree.root, tree.current_node = data["turns"], root, current
        tree.next_node_id = data["next_node_id"]
        if frozen_ids(tree) != data.get("frozen_node_ids"):
            raise ValueError("Frozen branch state does not match topology")
        return tree

    def export_retrieval_tree(self, content_roles=("user",)):
        return retrieval(self, content_roles)

    def to_dict(self):
        return self.snapshot_state()

    def save(self, filepath, save_conversation=True):
        # Explicit filesystem action, never a model operation. Applications can
        # use snapshot_state with their own atomic commit instead.
        Path(filepath).write_text(json.dumps(self.snapshot_state(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, filepath, *, llm=None):
        return cls.restore_state(json.loads(Path(filepath).read_text(encoding="utf-8")), llm=llm)

    def print_tree(self, **kwargs):
        from .visualize import print_detailed_tree
        print_detailed_tree(self, **kwargs)
