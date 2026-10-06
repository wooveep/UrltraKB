"""Verify the source and existing PageIndex location contracts before storage."""

from pathlib import Path

from pageindex.backend.input_package import materialize_pages
from pageindex.index.block_policy import BlockPolicy
from pageindex.index.page_parts_policy import PagePartsPolicy

from openkb.index_location import IndexLocation
from openkb.state import HashRegistry


def validate_document(doc: dict, location: IndexLocation, collection: str, doc_id: str) -> None:
    metadata = doc["metadata"]
    doc_type = doc.get("doc_type")
    if not isinstance(doc_type, str):
        raise ValueError("Document requires a supported source type")
    kind = metadata.get("unit_kind")
    if doc_type in {"md", "markdown"} and kind is None:
        kind = "line"
    expected = {"pdf": "page", "okpi": "page", "okbi": "block", "md": "line", "markdown": "line"}
    if kind is None or kind != expected.get(doc_type):
        raise ValueError("Unsupported or missing document unit kind")
    if kind != "line" and (
        type(metadata.get("unit_count")) is not int or metadata["unit_count"] < 1
    ):
        raise ValueError("Document requires a positive source unit count")
    if not isinstance(metadata.get("parser_policy"), str) or not metadata["parser_policy"]:
        raise ValueError("Document requires its parser policy")
    if doc["file_path"] != (Path(collection) / f"{doc_id}.{doc_type}").as_posix():
        raise ValueError("Managed source path does not match its document identity")
    source = location.input_path(doc["file_path"])
    if not source.is_file() or HashRegistry.hash_file(source) != metadata["source_digest"]:
        raise ValueError("Managed document input digest changed")
    pages = doc.get("pages")
    if pages is None:
        raise ValueError("New document indexes require a complete content cache")
    materialize_pages(pages, location.inputs_root / collection / doc_id, metadata)
    line_numbers = {page["page"] for page in pages} if kind == "line" else set()
    policy = (
        BlockPolicy(metadata)
        if metadata.get("unit_kind") == "block"
        else PagePartsPolicy(metadata)
        if metadata.get("notes_policy")
        else None
    )

    def verify(nodes):
        if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
            raise ValueError("Document structure must contain node objects")
        for node in nodes:
            if kind == "line":
                if (
                    type(node.get("line_num")) is not int
                    or node["line_num"] not in line_numbers
                    or "start_index" in node
                    or "end_index" in node
                ):
                    raise ValueError("Document tree line is missing from its source cache")
            else:
                start, end = node.get("start_index"), node.get("end_index")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 1 <= start <= end <= metadata["unit_count"]
                ):
                    raise ValueError("Document tree location exceeds its source units")
            if policy:
                anchor = node.get("anchor", {})
                candidate = {
                    **node,
                    "physical_index": anchor.get("unit"),
                    "structure": node.get("structure", "1"),
                }
                if not policy.valid(candidate):
                    raise ValueError("Document tree contains an unverified source anchor")
            verify(node.get("nodes", []))

    verify(doc.get("structure"))
