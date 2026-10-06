from typing import Any

from .protocol import BaseAdapter
from .document import DocumentTreeAdapter, PageIndexAdapter

_SKIP_KEYS = frozenset({"content", "text", "children", "nodes"})


class ChatIndexAdapter(BaseAdapter):
    def convert(self, chatindex_json: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        entities = {}
        root_entity_id = "__root__"
        conversation_title = chatindex_json.get("conversation_id") or "Conversation"
        entities[root_entity_id] = {
            "type": "conversation",
            "title": conversation_title,
            "conversation_id": chatindex_json.get("conversation_id"),
            "participants": chatindex_json.get("participants"),
            "metadata": {
                "conversation_id": chatindex_json.get("conversation_id"),
                "participants": chatindex_json.get("participants"),
            },
        }

        def extract_topic(topic: dict[str, Any]) -> dict[str, Any]:
            topic_id = topic.get("node_id")
            messages = topic.get("messages", [])

            entities[topic_id] = {
                "type": "topic",
                "title": topic.get("title"),
                "summary": topic.get("summary", ""),
                "messages": messages,
                "msg_start": topic.get("msg_start"),
                "msg_end": topic.get("msg_end"),
            }

            children = None
            subtopics = topic.get("subtopics", [])
            if subtopics:
                children = {st.get("node_id"): extract_topic(st) for st in subtopics}

            return {
                "type": "object" if children else "leaf",
                "attrs": {
                    "title": topic.get("title"),
                    "summary": topic.get("summary"),
                    "msg_start": topic.get("msg_start"),
                    "msg_end": topic.get("msg_end"),
                },
                "entity_id": topic_id,
                "children": children,
            }

        tree_structure = {
            "type": "object",
            "attrs": {
                "title": conversation_title,
                "conversation_id": chatindex_json.get("conversation_id"),
                "participants": chatindex_json.get("participants"),
            },
            "entity_id": root_entity_id,
            "children": {t["node_id"]: extract_topic(t) for t in chatindex_json.get("topics", [])},
        }

        return tree_structure, entities


class GenericAdapter(BaseAdapter):
    def convert(self, generic_json: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        entities = {}

        def extract_value(value: Any, path: str = "") -> dict[str, Any]:
            if isinstance(value, dict):
                if "content" in value or "text" in value:
                    entity_id = f"entity_{path}".replace("/", "_")
                    entities[entity_id] = {
                        "type": value.get("type", "content"),
                        "content": value.get("content") or value.get("text"),
                    }
                    return {
                        "type": "leaf",
                        "summary": value.get("summary"),
                        "attrs": {k: v for k, v in value.items() if k not in _SKIP_KEYS},
                        "entity_id": entity_id,
                    }

                children_dict = value.get("children") or value.get("nodes")
                if children_dict:
                    children = {}
                    if isinstance(children_dict, dict):
                        children = {k: extract_value(v, f"{path}/{k}") for k, v in children_dict.items()}
                    elif isinstance(children_dict, list):
                        children = {str(i): extract_value(v, f"{path}/{i}") for i, v in enumerate(children_dict)}

                    return {
                        "type": "object",
                        "summary": value.get("summary"),
                        "attrs": {k: v for k, v in value.items() if k not in _SKIP_KEYS},
                        "children": children,
                    }

            return {"type": "leaf"}

        tree_structure = extract_value(generic_json, "root")
        return tree_structure, entities
