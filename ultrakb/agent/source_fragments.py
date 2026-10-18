"""Keep source fences and table rows intact, with located original continuation headers."""

from collections import defaultdict
from dataclasses import dataclass

from ultrakb.agent.create_requests import selection_key
from ultrakb.agent.page_response import PageResponseError
from ultrakb.content_blocks import markdown_layout


def selection_windows(reader, windows):
    """Adapt document windows only when subdivision needs native selection coordinates."""
    import hashlib

    result = []
    for window in windows:
        if "native_location" in window:
            result.append(window)
            continue
        selected, location = reader._document_selection(window["path"])
        offset = window["selection_offset"]
        if (
            selected["content"][offset : offset + len(window["content"])] != window["content"]
            or hashlib.sha256(reader._read(window["path"])).hexdigest() != window["sha256"]
        ):
            raise PageResponseError(
                "page_evidence_changed", "Document window differs from its source"
            )
        result.append(
            {**window, "native_location": location, "selection_length": len(selected["content"])}
        )
    return result


def fragment_selections(windows):
    """Coalesce only contiguous frozen scopes; a partial batch never reopens its source."""
    groups = defaultdict(list)
    for window in windows:
        groups[selection_key(window)].append(window)
    for values in groups.values():
        original, start, stop = None, 0, 0
        for window in sorted(values, key=lambda w: w["selection_offset"]):
            offset = window["selection_offset"]
            end = offset + len(window["content"])
            if original is not None and offset > stop:
                yield original, start, stop
                original = None
            if original is None:
                original, start = window, offset
            stop = max(stop, end)
        if original is not None:
            yield original, start, stop


@dataclass(frozen=True)
class FragmentLayout:
    protected: tuple[tuple[int, int, str], ...]
    tables: tuple[tuple[int, int, int, str], ...]

    def containing(self, offset):
        return next((part for part in self.protected if part[0] < offset < part[1]), None)

    def context(self, window):
        offset = window["selection_offset"]
        if not window["path"].endswith(".md"):
            return window  # Native packages already carry qualified header context.
        contexts = [
            {
                "kind": "original_markdown_table_header",
                "text": header,
                "path": window["path"],
                "selection_span": [start, header_end],
            }
            for start, header_end, end, header in self.tables
            if header_end <= offset < end
        ]
        return (
            {**window, "display_context": [*window.get("display_context", []), *contexts]}
            if contexts
            else window
        )


def fragment_layouts(windows):
    grouped = defaultdict(list)
    for window in windows:
        grouped[selection_key(window)].append(window)
    result = {}
    for key, values in grouped.items():
        # All offsets are in the native selection, including partial batches.
        # Parse disjoint runs separately rather than treating an unread gap as text.
        protected, tables = [], []
        for _, start, stop in fragment_selections(values):
            fragments = sorted(
                (w for w in values if start <= w["selection_offset"] < stop),
                key=lambda w: w["selection_offset"],
            )
            parts, cursor = [], start
            for window in fragments:
                parts.append(window["content"][max(0, cursor - window["selection_offset"]) :])
                cursor = max(cursor, window["selection_offset"] + len(window["content"]))
            text = "".join(parts)
            layout = _layout(text, start)
            protected.extend(layout.protected)
            tables.extend(layout.tables)
        result[key] = FragmentLayout(tuple(protected), tuple(tables))
    return result


def _layout(text, start):
    tokens, offsets = markdown_layout(text)
    offsets = [offset + start for offset in offsets]
    protected, tables = [], []
    for token in tokens:
        if token.map and token.type in {"fence", "code_block"}:
            protected.append((offsets[token.map[0]], offsets[token.map[1]], "excerpt"))
        elif token.map and token.type == "table_open":
            start, end = offsets[token.map[0]], offsets[token.map[1]]
            head = next(
                part
                for part in tokens
                if part.type == "thead_open"
                and part.map
                and token.map[0] <= part.map[0] < part.map[1] <= token.map[1]
            )
            header_end = offsets[min(head.map[1] + 1, token.map[1])]
            # offsets are global to the selection, text is local to this run.
            base = offsets[0]
            tables.append((start, header_end, end, text[start - base : header_end - base]))
            protected.append((start, header_end, "table_header"))
        elif token.map and token.type == "tr_open":
            protected.append((offsets[token.map[0]], offsets[token.map[1]], "table_row"))
    return FragmentLayout(
        tuple(sorted(protected, key=lambda part: (part[0], -part[1]))), tuple(tables)
    )


def iter_fragments(reader, original, *, budget, layout, start=0, stop=None):
    """Use actual reader-owned coordinates while moving cuts outside protected source bytes."""
    location = {k: v for k, v in original["native_location"].items() if k != "offset"}
    offset = start
    length = original["selection_length"] if stop is None else stop

    def read(offset, count, *, exact=False):
        iterator = reader.iter_selection(
            original["path"],
            {**location, "offset": offset},
            budget=count,
            allow_suffix=offset > 0,
            **({"content_budget": True} if exact else {}),
        )
        try:
            return next(iterator)
        finally:
            iterator.close()

    while offset < length:
        count = min(budget, length - offset)
        try:
            window = read(offset, count)
        except PageResponseError as error:
            if error.code != "page_invalid_continuation" or not original["path"].endswith(
                ".content.json"
            ):
                raise
            window = read(offset, count, exact=True)
        if not window["content"] and original["path"].endswith(".content.json"):
            # Historical native projection counted its metadata against the text
            # budget. A short final tail can fit the complete request but produce
            # no reader output. Keep successful old slices and repair only this tail.
            window = read(offset, count, exact=True)
        end = offset + len(window["content"])
        if protected := layout.containing(end):
            protected_start, protected_stop, _ = protected
            desired_end = protected_start if offset < protected_start else protected_stop
            if desired_end > length:
                raise PageResponseError(
                    "page_invalid_continuation", "Literal crosses the frozen scope"
                )
            window = read(offset, desired_end - offset, exact=True)
            end = offset + len(window["content"])
        if end <= offset:
            raise PageResponseError("page_invalid_continuation", "Source fragment made no progress")
        yield layout.context({**window, "source_role": original.get("source_role", "current")})
        offset = end


def require_splittable(window, layout, *, prefix="page_create"):
    start = window["selection_offset"]
    end = start + len(window["content"])
    protected = next(
        (part for part in layout.protected if part[0] == start and part[1] == end), None
    )
    if protected is not None:
        _, _, kind = protected
        raise PageResponseError(
            f"{prefix}_indivisible_{kind}",
            f"Necessary {kind} at {window['path']} selection characters {start}:{end} "
            "exceeds the complete request capacity; original retained",
            field_errors=(
                {
                    "path": window["path"],
                    "native_location": window["native_location"],
                    "selection_span": [start, end],
                    "kind": kind,
                },
            ),
        )


def split_fragment_batch(batch, reader, layouts, *, prefix="page_create"):
    """Split one failed contribution, retaining native positions and every required byte."""
    if len(batch) > 1:
        middle = len(batch) // 2
        return [batch[:middle], batch[middle:]]
    if batch:
        original = batch[0]
        start = original["selection_offset"]
        length = len(original["content"])
        parts = list(
            iter_fragments(
                reader,
                original,
                budget=max(1, length // 2),
                layout=layouts[selection_key(original)],
                start=start,
                stop=start + length,
            )
        )
        if len(parts) >= 2 and all(0 < len(part["content"]) < length for part in parts):
            return [[part] for part in parts]
        location = f"{original['path']} selection characters {start}:{start + length}"
    else:
        location = "the required fixed page context"
    raise PageResponseError(
        f"{prefix}_indivisible_output",
        f"Output cannot complete for {location}; "
        "no legal smaller source fragment; original retained",
        field_errors=tuple(
            {"path": window["path"], "native_location": window["native_location"]}
            for window in batch
        ),
    )
