from typing import Any

from .protocol import BaseAdapter
from .document import DocumentTreeAdapter, PageIndexAdapter
from .chatindex import ChatIndexAdapter

_SKIP_KEYS = frozenset({"content", "text", "children", "nodes"})


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
