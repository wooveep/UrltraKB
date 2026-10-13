"""Keep native section boundaries when locating or dividing original evidence."""

from collections.abc import Callable, Iterator


def expand_navigation_locations(locations: list[dict], key: str, navigation: list[dict]) -> str:
    """Include enclosing sections and both sections sharing a hit's boundary page."""
    ranges = {
        (node["start_index"], node["end_index"])
        for node in navigation
        if type(node.get("start_index")) is int
        and type(node.get("end_index")) is int
        and 1 <= node["start_index"] < node["end_index"]
    }
    selected = set()
    for location in locations:
        number = int(location[key])
        enclosing = {(a, b) for a, b in ranges if a <= number <= b}
        # A heading-only node must not hide the following section text. A true
        # subsection replaces its ancestors; overlapping siblings both remain.
        sections = {
            (a, b)
            for a, b in enclosing
            if not any(a <= c <= d <= b and (a, b) != (c, d) for c, d in enclosing)
        }
        selected.update(sections or {(number, number)})
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in sorted(selected))


def split_document_window(selected: dict, window: dict) -> list[dict]:
    """Divide once near the midpoint, retaining exact native coverage and context."""
    from openkb.agent.compile_messages import render_original_window
    from openkb.agent.evidence_payload import (
        following_evidence,
        image_layout,
        preceding_evidence,
        slice_evidence,
    )
    from openkb.agent.page_response import PageResponseError
    from openkb.content_blocks import markdown_layout

    text = selected["content"]
    start = window["selection_offset"]
    content = window["content"]
    end = start + len(content)
    if type(start) is not int or not 0 <= start < end <= len(text) or text[start:end] != content:
        raise PageResponseError("document_summary_reader_invalid", "Split input changed")
    first = start + len(content) - len(content.lstrip()) + 1
    last = start + len(content.rstrip()) - 1
    if first > last:
        raise PageResponseError("page_truncated_response", "Original batch cannot be divided")
    layout = markdown_layout(text)
    tables = selected.get("_qualified_page_tables", selected.get("_qualified_tables", []))
    images = image_layout(selected)
    tokens, offsets = layout
    boundaries = {
        offsets[token.map[1]]
        for token in tokens
        if token.map
        and token.level == 0
        and max(first, start + len(content) // 4)
        <= offsets[token.map[1]]
        <= min(last, end - len(content) // 4)
    }
    midpoint = min(max((start + end) // 2, first), last)
    cut = min(boundaries, key=lambda n: (abs(n - midpoint), n)) if boundaries else midpoint
    children = []
    for a, b in ((start, cut), (cut, end)):
        context = []
        if a:
            context.append(
                preceding_evidence(selected, a, 1200, layout=layout, tables=tables, images=images)
            )
        if b < len(text):
            context.append(
                following_evidence(selected, b, 1200, layout=layout, tables=tables, images=images)
            )
        children.append(
            {
                **{key: window[key] for key in ("path", "sha256", "source_role")},
                **render_original_window(
                    slice_evidence(selected, a, b, layout=layout, tables=tables, images=images),
                    selected,
                ),
                "selection_offset": a,
                "supplementary_context": [render_original_window(c, selected) for c in context],
            }
        )
    return children


def iter_document_windows(reader, *, fits: Callable[[dict], bool]) -> Iterator[dict]:
    """Read the entire fixed document using actual reader selection offsets."""
    import hashlib
    import json

    from openkb.agent.compile_evidence import MAX_EVIDENCE_CHARS
    from openkb.agent.compile_messages import render_original_window
    from openkb.agent.evidence_payload import (
        following_evidence,
        image_layout,
        preceding_evidence,
        project_evidence,
    )
    from openkb.agent.page_response import PageResponseError
    from openkb.content_blocks import markdown_layout
    from openkb.source_search import original_units

    path = reader.original_path
    raw = reader._source(path)
    selected, location = reader._document_selection(path)
    # Only known blank originals can be skipped, after validating their readers.
    if not any(text.strip() for _, text in original_units(raw)):
        return
    digest = hashlib.sha256(reader._read(path)).hexdigest()
    layout = markdown_layout(selected["content"])
    tables = selected.get("_qualified_page_tables", selected.get("_qualified_tables", []))
    images = image_layout(selected)
    offset = 0
    while offset < len(selected["content"]):
        from openkb.llm_execution import check_model_stop

        check_model_stop()
        context_chars = 1200
        context = (
            [
                render_original_window(
                    preceding_evidence(
                        selected, offset, context_chars, layout=layout, tables=tables, images=images
                    ),
                    selected,
                )
            ]
            if offset
            else []
        )
        identity = {
            "path": path,
            "sha256": digest,
            "source_role": "current",
            "selection_offset": offset,
            "supplementary_context": context,
        }

        def with_context(payload):
            end = offset + len(payload["content"])
            after = (
                [
                    render_original_window(
                        following_evidence(
                            selected,
                            end,
                            context_chars,
                            layout=layout,
                            tables=tables,
                            images=images,
                        ),
                        selected,
                    )
                ]
                if end < len(selected["content"])
                else []
            )
            return {
                **identity,
                **render_original_window(payload, selected),
                "supplementary_context": [*context, *after],
            }

        while True:
            projected = project_evidence(
                selected,
                offset=offset,
                fits=lambda payload: len(payload["content"]) <= MAX_EVIDENCE_CHARS
                and len(json.dumps(payload, ensure_ascii=False)) <= MAX_EVIDENCE_CHARS * 2
                and fits(with_context(payload)),
                allow_partial=True,
                layout=layout,
                tables=tables,
                images=images,
            )
            if projected["content"] or context_chars <= 32:
                break
            context_chars = max(32, context_chars // 2)
            if offset:
                context = [
                    render_original_window(
                        preceding_evidence(
                            selected,
                            offset,
                            context_chars,
                            layout=layout,
                            tables=tables,
                            images=images,
                        ),
                        selected,
                    )
                ]
        if not projected["content"]:
            raise PageResponseError(
                "document_summary_capacity_exceeded", "Original window made no progress"
            )
        continuation = projected.get("continuation")
        if continuation is not None and (
            type(continuation) is not int
            or continuation != offset + len(projected["content"])
            or not offset < continuation <= len(selected["content"])
        ):
            raise PageResponseError("document_summary_reader_invalid", "Invalid continuation")
        if continuation is None and offset + len(projected["content"]) != len(selected["content"]):
            raise PageResponseError("document_summary_reader_invalid", "Original tail unread")
        window = with_context(projected)
        # Native blank pages or whitespace tails retain their coordinates in
        # the reader, but contain no section for the model to summarize.
        if window["content"].strip():
            yield window
        offset = len(selected["content"]) if continuation is None else continuation
