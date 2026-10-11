"""Keep native section boundaries when locating or dividing original evidence."""


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
    from openkb.agent.evidence_payload import following_evidence, preceding_evidence, slice_evidence
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
            context.append(preceding_evidence(selected, a, 1200, layout=layout))
        if b < len(text):
            context.append(following_evidence(selected, b, 1200, layout=layout))
        children.append(
            {
                **{key: window[key] for key in ("path", "sha256", "source_role")},
                **render_original_window(slice_evidence(selected, a, b, layout=layout), selected),
                "selection_offset": a,
                "supplementary_context": [render_original_window(c, selected) for c in context],
            }
        )
    return children
