"""Native reader capabilities and executable actions derived from frozen source bytes."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from openkb.application.query_views import QueryView, read_selected_file

CATALOG_ACTION = {"tool": "read_file", "arguments": {"path": "index.md"}}


class ReaderFailure(ValueError):
    def __init__(self, code: str, message: str, action=None):
        self.code, self.message = code, message
        self.action = action or CATALOG_ACTION
        super().__init__(message)


def native_source(view: QueryView, path: str) -> dict:
    """Validate the retained representation; file extensions alone confer no capability."""
    name = PurePosixPath(path).name
    doc = name.removesuffix(".content.json").removesuffix(".json").removesuffix(".md")
    data = read_selected_file(view, path)
    actions = {}
    ranges: dict[str, object] = {}
    if path.endswith(".content.json"):
        from openkb.text_source import read_text_selection

        source = read_text_selection(json.loads(data))
        kind = "worksheet" if source.get("sheet") else source["unit_kind"]
        ranges["chars"] = f"0:{source['characters']}"
        actions["get_text_content"] = {
            "doc_name": doc,
            "chars": f"0:{min(source['characters'], 12000)}",
            "offset": 0,
        }
        if count := source.get("block_count"):
            ranges["blocks"] = f"1-{count}"
            actions["get_block_content"] = {"doc_name": doc, "blocks": "1", "offset": 0}
        if source.get("sheet"):
            coordinates = list(
                dict.fromkeys(
                    origin["sheet_cell"]["cell"]["coordinate"]
                    for origin in source["origin_locators"]
                    if origin.get("sheet_cell")
                )
            )
            ranges["cells"] = coordinates
            if coordinates:
                actions["get_cell_content"] = {
                    "doc_name": doc,
                    "cells": coordinates[0],
                    "offset": 0,
                }
        preferred = next(reversed(actions))
    elif path.endswith(".json"):
        from openkb.source_pages import read_page_selection, select_page_part

        source = read_page_selection(json.loads(data))
        if source["unit_kind"] != "page" or not source["page_range"]:
            raise ReaderFailure("content_not_found", "No verified native pages in this source.")
        slides = any("parts" in unit for unit in source["units"])
        if slides:
            select_page_part(source, "body")  # All slides must have a verified partition.
        kind = "slide" if slides else "page"
        coordinate = "slides" if slides else "pages"
        preferred = "get_slide_content" if slides else "get_page_content"
        ranges[coordinate] = f"1-{source['pages']}"
        actions[preferred] = {"doc_name": doc, coordinate: "1"}
        if slides:
            actions[preferred]["part"] = "all"
    else:
        data.decode("utf-8")
        kind, preferred = "text", "read_file"
        actions[preferred] = {"path": path}
    return {
        "doc_name": doc,
        "path": path,
        "unit_kind": kind,
        "legal_ranges": ranges,
        "readers": actions,
        "reading_action": {"tool": preferred, "arguments": actions[preferred]},
        "source_revision_id": view.source_revisions_by_path.get(path),
        "content_digest": view.files[path],
    }


def source_capabilities(view: QueryView) -> dict[str, dict]:
    result = {}
    for path in sorted(view.files):
        if PurePosixPath(path).parent.as_posix() != "sources" or not path.endswith(
            (".md", ".json")
        ):
            continue
        stem = path.removesuffix(".md")
        if path.endswith(".md") and any(
            stem + suffix in view.files for suffix in (".json", ".content.json")
        ):
            continue
        value = native_source(view, path)
        result[value["doc_name"]] = value
    return result


def reading_action(source: dict, location: dict | None = None) -> dict:
    if not location:
        return source["reading_action"]
    for selector, tool in (
        ("cells", "get_cell_content"),
        ("blocks", "get_block_content"),
        ("chars", "get_text_content"),
        ("pages", "get_page_content"),
        ("slides", "get_slide_content"),
    ):
        if selector not in location:
            continue
        argument = selector
        if selector == "pages" and source["unit_kind"] == "slide":
            tool, argument = "get_slide_content", "slides"
        if tool in source["readers"]:
            return {
                "tool": tool,
                "arguments": {
                    **source["readers"][tool],
                    argument: location[selector],
                    **(
                        {"offset": location["offset"]}
                        if "offset" in location and "offset" in source["readers"][tool]
                        else {}
                    ),
                    **(
                        {"part": location["part"]}
                        if "part" in location and tool == "get_slide_content"
                        else {}
                    ),
                },
            }
    return source["reading_action"]


def require_reader(sources: dict[str, dict], doc_name: str, tool: str) -> dict:
    source = sources.get(doc_name)
    if source is None:
        raise ReaderFailure("content_not_found", "Document not found in this reading snapshot.")
    if tool not in source["readers"]:
        raise ReaderFailure(
            "tool_not_applicable",
            "This source uses a different native reader.",
            source["reading_action"],
        )
    return source
