"""Prepare immutable SQL rows before a host begins bounded write transactions."""

import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .json_values import validate_json


@dataclass(frozen=True)
class PreparedTree:
    tree_id: str
    root_id: str
    timestamp: int
    meta_json: str | None
    nodes: tuple[tuple, ...]
    entities: tuple[tuple, ...]


def prepare_tree(tree: dict[str, Any], entities=None, meta=None) -> PreparedTree:
    """Use the same node/path protocol as TreeDB's standalone ingestion."""
    for value in (tree, entities, meta):
        validate_json(value)
    if not isinstance(tree, dict) or entities is not None and not isinstance(entities, dict):
        raise ValueError("A tree requires an object root and an entity map")
    if meta is not None and not isinstance(meta, dict):
        raise ValueError("Tree metadata must be a JSON object")
    tree_id, root_id = str(uuid4()), tree.get("node_id") or str(uuid4())
    timestamp = int(time.time() * 1000)
    entity_rows, node_rows, seen = [], [], set()
    for entity_id, payload in (entities or {}).items():
        if not isinstance(payload, dict):
            raise ValueError("Entity payloads must be JSON objects")
        entity_rows.append((tree_id, entity_id, payload.get("type", "unknown"),
                            json.dumps(payload, ensure_ascii=False, allow_nan=False), timestamp))

    def visit(data, node_id, parent_id, parent_path, parent_depth, slot, ordinal):
        if not isinstance(data, dict) or not isinstance(node_id, str) or not node_id or node_id in seen:
            raise ValueError("Tree node identities must be nonempty and unique within their tree")
        seen.add(node_id)
        depth = parent_depth + 1 if parent_id else 0
        path = f"{parent_path}/{ordinal:012d}" if parent_path else "/r"
        kind = data.get("type", "object")
        if kind not in {"object", "array", "leaf"}:
            raise ValueError("Unknown tree node type")
        entity_id = data.get("entity_id")
        if data.get("attrs") is not None and not isinstance(data["attrs"], dict):
            raise ValueError("Tree node attributes must be JSON objects")
        if entity_id is not None and entity_id not in (entities or {}):
            raise ValueError("Tree node refers to a missing tree-local entity")
        node_rows.append((tree_id, node_id, parent_id, slot,
                          {"object": 0, "array": 1, "leaf": 2}[kind], depth, path,
                          data.get("entity_type"), entity_id,
                          json.dumps(data["attrs"], ensure_ascii=False) if data.get("attrs") else None,
                          timestamp, timestamp))
        children = data.get("children")
        if children is None:
            return
        if not isinstance(children, (dict, list)):
            raise ValueError("Tree children must be an object or ordered array")
        pairs = children.items() if isinstance(children, dict) else enumerate(children)
        for index, (key, child) in enumerate(pairs):
            if not isinstance(child, dict):
                raise ValueError("Tree children must be objects")
            visit(child, child.get("node_id") or str(uuid4()), node_id, path, depth, str(key), index)

    visit(tree, root_id, None, None, -1, None, 0)
    return PreparedTree(tree_id, root_id, timestamp,
                        json.dumps(meta, ensure_ascii=False) if meta else None,
                        tuple(node_rows), tuple(entity_rows))
