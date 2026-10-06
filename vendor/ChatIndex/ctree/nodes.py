"""Ordered topic and exchange nodes. Coordinates are exchange ordinals."""
from dataclasses import dataclass, field


@dataclass(eq=False)
class Node:
    children: list = field(default_factory=list)
    parent: object = field(default=None, repr=False)
    sub_node_count: int = 0
    node_id: str = ""
    ordinal: int = 0

    def update_sub_node_count(self):
        self.sub_node_count = len(self.children)
        for ordinal, child in enumerate(self.children):
            child.ordinal, child.parent = ordinal, self
        if self.parent:
            self.parent.update_sub_node_count()

    def is_leaf(self):
        return not self.children


@dataclass(eq=False)
class MessageNode(Node):
    user_message: dict = field(default_factory=dict)
    assistant_message: dict = field(default_factory=dict)
    system_message: dict | None = None
    message_index: int = 0
    turn_id: str = ""

    @property
    def start_index(self):
        return self.message_index

    @property
    def end_index(self):
        return self.message_index + 1

    def to_dict(self):
        return {"node_id": self.node_id, "ordinal": self.ordinal, "kind": "exchange",
                "turn_id": self.turn_id, "start_turn": self.start_index,
                "end_turn": self.end_index, "children": []}


@dataclass(eq=False)
class TopicNode(Node):
    topic_name: str = ""
    summary: str = ""
    start_index: int = 0
    end_index: int = 0

    def get_message_count(self):
        return self.end_index - self.start_index

    def to_dict(self):
        return {"node_id": self.node_id, "ordinal": self.ordinal, "kind": "topic",
                "title": self.topic_name, "summary": self.summary,
                "start_turn": self.start_index, "end_turn": self.end_index,
                "children": [child.to_dict() for child in self.children]}


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)
