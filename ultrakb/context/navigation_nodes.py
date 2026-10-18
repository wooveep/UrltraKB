"""Deterministic source navigation shared by live preparation and sealed revisions."""

import json
from bisect import bisect_left, bisect_right


def validate_native_ranges(structure: list[dict], ordinals: list[int]) -> None:
    """Every indexed interval must resolve completely in its fixed original input."""
    pending = list(structure)
    while pending:
        node = pending.pop()
        start, end = node.get("start_index"), node.get("end_index")
        if (
            type(start) is not int
            or type(end) is not int
            or not 1 <= start <= end
            or bisect_right(ordinals, end) - bisect_left(ordinals, start) != end - start + 1
        ):
            raise ValueError("Indexed navigation does not resolve its complete native source range")
        pending.extend(node.get("nodes", []))


def validate_index_positions(data: bytes, kind: str, structure: list[dict]) -> None:
    if kind == "pages":
        from ultrakb.source_pages import read_page_selection

        units = read_page_selection(json.loads(data))["units"]
        validate_native_ranges(structure, [page["page"] for page in units])
    elif kind == "text":
        from ultrakb.text_source import read_text_selection

        raw = json.loads(data)
        if read_text_selection(raw).get("block_count"):
            from ultrakb.block_package import read_block_selection

            units = read_block_selection(raw)["units"]
            validate_native_ranges(structure, [block["ordinal"] for block in units])


def leaf(title: str, reference: dict, native: dict) -> dict:
    reference = {**reference, "native_location": native}
    return {
        "title": title,
        "rel_path": title,
        "is_dir": False,
        "source_ref": reference,
        "content": "Original source locator; reread before use.",
    }


def indexed_nodes(structure: list[dict], reference: dict, kind: str) -> dict:
    nodes = {}
    for ordinal, node in enumerate(structure):
        start, end = node.get("start_index"), node.get("end_index")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError("Published navigation has invalid native positions")
        title = (
            reference["doc_name"]
            + " / "
            + str(node.get("title", ""))
            + " — navigation: "
            + str(node.get("summary", ""))[:800]
        )
        children = indexed_nodes(node.get("nodes", []), reference, kind)
        key = str(node.get("node_id", ordinal))
        nodes[key] = (
            {"title": title, "rel_path": title, "is_dir": True, "children": children}
            if children
            else leaf(title, reference, {kind: f"{start}-{end}"})
        )
    return nodes


def compact_navigation(node: dict) -> dict:
    """Keep every result locator and ancestor title, dropping unary structure only."""
    node = dict(node)
    if "children" in node:
        node["children"] = {
            key: compact_navigation(child) for key, child in node["children"].items()
        }
    while node.get("is_dir") and not node.get("source_ref") and len(node.get("children", {})) == 1:
        child = next(iter(node["children"].values()))
        title = node["title"] + " / " + child["title"]
        node = {**node, **child, "title": title, "rel_path": title}
        if not child.get("children"):
            node.pop("children", None)
    return node


def text_nodes(raw: dict, reference: dict) -> dict:
    text, nodes = raw["text"], {}
    origins = raw.get("origins", [])
    cells = [o for o in origins if o.get("sheet_cell")]
    if cells:
        for start in range(0, len(cells), 12):
            chunk = cells[start : start + 12]
            coordinates = [o["sheet_cell"]["cell"]["coordinate"] for o in chunk]
            a, b = chunk[0]["normalized_span"][0], chunk[-1]["normalized_span"][1]
            nodes[str(start)] = leaf(
                reference["doc_name"] + " / " + ",".join(coordinates) + " / " + text[a:b][:500],
                reference,
                {"cells": ",".join(coordinates)},
            )
    else:
        for start in range(0, len(text), 4000):
            end = min(len(text), start + 4000)
            nodes[str(start)] = leaf(
                reference["doc_name"] + f" / chars {start}:{end} / " + text[start : start + 500],
                reference,
                {"chars": f"{start}:{end}"},
            )
    return nodes


def source_nodes(data: bytes, kind: str, reference: dict, structure: list[dict]) -> dict:
    """Preserve the existing page, block, cell and character navigation policies."""
    doc_name = reference["doc_name"]
    if kind == "text":
        from ultrakb.text_source import read_text_selection

        raw = json.loads(data)
        source = read_text_selection(raw)
        if not source.get("block_count"):
            return text_nodes(raw, reference)
        from ultrakb.block_package import read_block_selection

        blocks = read_block_selection(raw)["units"]
        validate_native_ranges(structure, [block["ordinal"] for block in blocks])
        return indexed_nodes(structure, reference, "blocks") or {
            str(block["ordinal"]): leaf(
                doc_name + " / " + block["content"][:400],
                reference,
                {"blocks": str(block["ordinal"])},
            )
            for block in blocks
        }
    if kind == "pages":
        from ultrakb.source_pages import read_page_selection

        pages = read_page_selection(json.loads(data))["units"]
        validate_native_ranges(structure, [page["page"] for page in pages])
        return indexed_nodes(structure, reference, "pages") or {
            str(page["page"]): leaf(
                doc_name + " / physical page " + str(page["page"]) + " / " + page["content"][:500],
                reference,
                {"pages": str(page["page"])},
            )
            for page in pages
        }
    if kind != "markdown":
        raise ValueError("Unknown normalized source content protocol")
    text = data.decode("utf-8")
    return {"source": leaf(doc_name + " / " + text[:500], reference, {"chars": f"0:{len(text)}"})}
