"""Lossless document storage plus an ordered navigation projection.

Locators keep their source names and units. Their application-specific meaning is
validated by the caller; this boundary only requires explicit source positions.
"""
import copy
from typing import Any

from .protocol import BaseAdapter
from contextdb.core.json_values import validate_json


class DocumentContractError(ValueError):
    """A document is missing source identity or explicit content positions."""


def _has_location(node):
    if any(isinstance(node.get(key), (dict, list)) and node[key] for key in (
        "source_locator", "source_locators", "source_spans", "origin_locators", "locator",
    )):
        return True
    if type(node.get("line_num")) is int and node["line_num"] > 0:
        return True
    start, end = node.get("start_index"), node.get("end_index")
    return type(start) is int and type(end) is int and 0 < start <= end


class DocumentTreeAdapter(BaseAdapter):
    def convert(self, document_json: dict[str, Any]):
        if not isinstance(document_json, dict) or not isinstance(document_json.get("doc_name"), str) or not document_json["doc_name"]:
            raise DocumentContractError("Document requires a nonempty doc_name source identity")
        if not isinstance(document_json.get("structure"), list):
            raise DocumentContractError("Document requires an ordered structure")
        # Reject non-JSON values rather than silently losing extensions on disk.
        validate_json(document_json)
        entities, seen = {}, {"__document__"}

        def extract(node, ordinal):
            if not isinstance(node, dict):
                raise DocumentContractError("Document node must be an object")
            identity = node.get("node_id")
            if not isinstance(identity, str) or not identity or identity in seen:
                raise DocumentContractError("Document nodes require unique source node_id")
            seen.add(identity)
            if not isinstance(node.get("title"), str) or not _has_location(node):
                raise DocumentContractError(f"Document node {identity!r} requires title and source locator")
            children = node.get("nodes", [])
            if not isinstance(children, list):
                raise DocumentContractError("Document children must be ordered")
            entities[identity] = {
                "type": "section", "title": node["title"], "summary": node.get("summary", ""),
                "text": node.get("text", ""), "source": copy.deepcopy(node),
            }
            attrs = {k: copy.deepcopy(v) for k, v in node.items() if k not in {"nodes", "text"}}
            attrs["ordinal"] = ordinal
            return {"node_id": identity, "type": "object" if children else "leaf",
                    "entity_id": identity, "attrs": attrs,
                    "children": [extract(child, i) for i, child in enumerate(children)]}

        entities["__document__"] = {
            "type": "document", "title": document_json["doc_name"],
            "document": copy.deepcopy(document_json),
        }
        return {
            "node_id": "__document__", "type": "object", "entity_id": "__document__",
            "attrs": {k: copy.deepcopy(v) for k, v in document_json.items() if k != "structure"},
            "children": [extract(node, i) for i, node in enumerate(document_json["structure"])],
        }, entities


PageIndexAdapter = DocumentTreeAdapter
