"""Bounded original context for code split across document paragraphs."""

import re
from dataclasses import asdict, replace

from openkb.evidence import evidence_bounds


def _scope(location):
    result = [location.get("kind"), location.get("headings", [])]
    while isinstance(location.get("attachment"), dict):
        attachment = location["attachment"]
        result.append([attachment["part"], attachment["name"], attachment["blob"]])
        location = attachment["position"]
        result.append(location.get("headings", []))
    return result


def preceding_slices(reader, reference, identity, blocks, *, max_blocks, max_chars):
    """Nearest preceding complete paragraphs, never crossing a structural scope."""
    if type(max_blocks) is not int or max_blocks <= 0:
        raise ValueError("Context requires a positive block bound")
    current, _ = evidence_bounds(reference, identity, blocks, max_chars)
    if current.kind not in {"paragraph", "code"}:
        return
    ordered = tuple(blocks.values())
    remaining = max_chars
    for block in reversed(ordered[max(0, current.order - max_blocks) : current.order]):
        if block.kind not in {"paragraph", "code"} or _scope(block.location) != _scope(
            current.location
        ):
            break
        if block.chars > remaining:
            break
        view = reader.read(
            replace(reference, block_id=block.id, start=0, end=block.chars or None),
            max_chars=max(4096, block.chars),
        )
        remaining -= len(view.text)
        yield view


def _braces(text):
    """Ignore single-line quoted strings and comments; ambiguous quoting stops context."""
    result = []
    for line in text.splitlines():
        quoted, escaped = None, False
        for index, char in enumerate(line):
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif quoted:
                if char == quoted:
                    quoted = None
            elif char in "\"'":
                quoted = char
            elif char == "#" or line[index : index + 2] == "//":
                break
            elif char in "{}":
                result.append(char)
        if quoted:
            return None
    return result


def enclosing_code(reader, reference):
    """Restore the smallest balanced enclosing snippet for a standalone closing brace.

    This only supplies verbatim context. It does not classify a configuration
    block or invent a fact; the usual semantic review still determines support.
    """
    preceding = getattr(reader, "preceding", None)
    if preceding is None:
        return []
    view = reader.read(reference, max_chars=max(4096, (reference.end or 0) - reference.start))
    if not re.fullmatch(r"\s*}\s*;?\s*", view.text):
        return []
    depth, selected = 1, []
    for neighbor in preceding(reference, max_blocks=32, max_chars=8192):
        braces = _braces(neighbor.text)
        if braces is None:
            return []
        selected.append(neighbor)
        for char in reversed(braces):
            depth += 1 if char == "}" else -1
            if depth == 0:
                return [
                    {
                        "reference": asdict(item.reference),
                        "relation": "enclosing_code",
                        "text": item.text,
                        "location": item.location,
                        "context": item.context,
                    }
                    for item in reversed(selected)
                ]
    return []
