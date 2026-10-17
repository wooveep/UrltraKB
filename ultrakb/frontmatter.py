"""Shared helpers for the YAML frontmatter blocks of UltraKB wiki pages.

Single source of truth for building, splitting, parsing, and mutating the
``---`` frontmatter used by summaries / concepts / entities. The closing
delimiter is always matched at the start of a line (``\\n---``), so a ``---``
that appears inside a quoted value never truncates the block — the failure
mode that ad-hoc ``text.find("---", 3)`` parsing was prone to.
"""

from __future__ import annotations

import json
import re

import yaml


def kv_line(key: str, value: str) -> str:
    """Render ``key: "value"`` with the value JSON-quoted (always single-line).

    JSON strings are a strict subset of YAML: always single-line, always
    correctly escaped (newlines, quotes, colons, control chars), and never
    auto-promoted to a block scalar.
    """
    return f"{key}: {json.dumps(value, ensure_ascii=False)}"


def list_line(key: str, items) -> str:
    """Render ``key: ["a", "b"]`` as JSON-style YAML (always single-line)."""
    return f"{key}: {json.dumps(list(items), ensure_ascii=False)}"


def block(lines: list[str]) -> str:
    """Assemble a complete frontmatter block (with delimiters + trailing blank)."""
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def parse_list_value(line: str) -> list[str] | None:
    """Parse the right-hand side of ``key: [...]`` into a list of strings.

    Returns ``None`` when the value cannot be interpreted as a list — callers
    treat that as "leave the frontmatter alone".
    """
    colon = line.find(":")
    if colon == -1:
        return None
    try:
        parsed = yaml.safe_load(line[colon + 1 :])
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(x) for x in parsed]


def split(text: str) -> tuple[str, str] | None:
    """Split ``text`` into ``(frontmatter_block, body)``.

    ``frontmatter_block`` includes both ``---`` delimiters and the newline that
    ends the closing delimiter line; ``body`` is everything after, so
    ``frontmatter_block + body == text`` exactly (lossless).

    Returns ``None`` when ``text`` has no well-formed frontmatter: no leading
    ``---`` or no line-anchored closing ``---``. Because the closing delimiter
    must start a line (``\\n---``), a ``---`` inside a quoted value is ignored.
    """
    if not text.startswith("---"):
        return None
    nl = text.find("\n---", 3)
    if nl == -1:
        return None
    after = text.find("\n", nl + 1)  # newline ending the closing '---' line
    if after == -1:
        return text, ""
    return text[: after + 1], text[after + 1 :]


def parse(text: str) -> dict:
    """Return the frontmatter as a dict (``{}`` when absent or malformed)."""
    parts = split(text)
    if parts is None:
        return {}
    fm_block = parts[0]
    inner = fm_block[3:]  # drop opening '---'
    close = inner.rfind("\n---")  # drop closing '---' line
    if close != -1:
        inner = inner[:close]
    try:
        data = yaml.safe_load(inner)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def set_line(fm_block: str, key: str, value: str) -> str:
    """Set or insert a single scalar ``key:`` line in a frontmatter block.

    Replaces an existing line for ``key``; otherwise inserts it right after the
    opening ``---``. A lambda replacement is used so values containing regex
    backrefs (``\\1``, ``\\g<…>``) are inserted literally.
    """
    line = kv_line(key, value)
    if re.search(rf"^{re.escape(key)}:", fm_block, flags=re.MULTILINE):
        return re.sub(
            rf"^{re.escape(key)}:.*", lambda _m: line, fm_block, count=1, flags=re.MULTILINE
        )
    return fm_block.replace("---\n", f"---\n{line}\n", 1)


def drop_line(fm_block: str, key: str) -> str:
    """Remove any ``key:`` line from a frontmatter block (no-op if absent)."""
    return re.sub(rf"^{re.escape(key)}:.*\n?", "", fm_block, flags=re.MULTILINE)


def alias_values(value: object, *, allow_scalar: bool = False) -> list[str]:
    """Validate literal identity labels without turning them into guessed paths."""
    if value is None:
        return []
    if allow_scalar and isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError("aliases must be a list of nonempty strings")
    return list(value)


def merge_aliases(text: str, proposed: object) -> str:
    """Merge accepted names, preserving body and unrelated frontmatter bytes.

    Replace the value's actual token span. A composed alias node points to its
    anchor definition, so its node positions cannot identify the reference.
    """
    if proposed is None:
        return text
    additions = alias_values(proposed)
    if not additions:
        return text
    parts = split(text)
    if parts is None:
        return block([list_line("aliases", list(dict.fromkeys(additions)))]) + text
    fm_block, body = parts
    start = fm_block.find("\n") + 1
    close = fm_block.rfind("\n---")
    if close and fm_block[close - 1] == "\r":
        close -= 1
    newline_style = "\r\n" if fm_block.startswith("---\r\n") else "\n"
    inner = fm_block[start:close]
    try:
        root = yaml.compose(inner)
        metadata = yaml.safe_load(inner)
        tokens = list(yaml.scan(inner))
    except yaml.YAMLError as error:
        raise ValueError("Cannot merge aliases into invalid frontmatter") from error
    if root is not None and not isinstance(root, yaml.MappingNode):
        raise ValueError("Cannot merge aliases into non-mapping frontmatter")
    fields = [(key, value) for key, value in root.value if key.value == "aliases"] if root else []
    if len(fields) > 1:
        raise ValueError("Duplicate aliases fields in frontmatter")
    previous_value = metadata.get("aliases") if isinstance(metadata, dict) else None
    previous = alias_values(previous_value, allow_scalar=True)
    merged = list(dict.fromkeys([*previous, *additions]))
    if isinstance(previous_value, list) and previous == merged:
        return text
    if not fields:
        if root is not None and root.flow_style:
            raise ValueError("Cannot add aliases to flow-style frontmatter")
        line = list_line("aliases", merged)
        return fm_block[:start] + line + newline_style + fm_block[start:] + body
    key, value = fields[0]
    key_token = next(
        (
            index
            for index, token in enumerate(tokens)
            if isinstance(token, yaml.tokens.ScalarToken)
            and token.end_mark.index == key.end_mark.index
        ),
        None,
    )
    preceding = (
        next(
            (
                token
                for token in reversed(tokens[:key_token])
                if isinstance(token, (yaml.tokens.KeyToken, yaml.tokens.ValueToken))
            ),
            None,
        )
        if key_token is not None
        else None
    )
    if not isinstance(preceding, yaml.tokens.KeyToken):
        raise ValueError("Cannot edit aliases through an unsupported YAML key reference")
    marker = next(
        (
            index
            for index, token in enumerate(tokens)
            if isinstance(token, yaml.tokens.ValueToken)
            and token.start_mark.index >= key.end_mark.index
        ),
        None,
    )
    if marker is None:
        raise ValueError("Cannot edit aliases without an explicit YAML value separator")
    first = tokens[marker + 1]
    # An implicit null has no value token; insert immediately after its colon.
    empty = value.tag == "tag:yaml.org,2002:null" and value.start_mark.index == value.end_mark.index
    begin = value.start_mark.index if empty else first.start_mark.index
    end_mark = first.end_mark if isinstance(first, yaml.tokens.AliasToken) else value.end_mark
    end = end_mark.index
    if end < begin:
        raise ValueError("Cannot locate aliases safely in frontmatter")
    removed_anchors = {
        token.value
        for token in tokens
        if isinstance(token, yaml.tokens.AnchorToken) and begin <= token.start_mark.index < end
    }
    if any(
        isinstance(token, yaml.tokens.AliasToken)
        and token.value in removed_anchors
        and not begin <= token.start_mark.index < end
        for token in tokens
    ):
        raise ValueError("Cannot change aliases whose YAML anchor is referenced by other metadata")
    separator = " " if empty and inner[begin - 1 : begin] == ":" else ""
    suffix = newline_style if end_mark.column == 0 and end < len(inner) else ""
    replacement = separator + json.dumps(merged, ensure_ascii=False) + suffix
    updated = inner[:begin] + replacement + inner[end:]
    return fm_block[:start] + updated + fm_block[close:] + body
