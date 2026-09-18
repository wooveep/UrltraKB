"""Lossless split positions between complete paragraphs, steps and code blocks."""

import re


def boundaries(text):
    result, offset, fence = [], 0, None
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        offset += len(line)
        marker = re.match(r"\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            else:
                continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if fence is not None or re.search(r"(?:\\|&&|\|\||\bAND|\bOR)\s*$", line, re.I):
            continue
        # An indented continuation belongs to its step; blank paragraphs and
        # complete unindented lines are usable boundaries, never arbitrary chars.
        if not following or not following[0].isspace() or not line.strip():
            result.append(offset)
    return result


def split_before(text, ceiling):
    return max((end for end in boundaries(text) if end <= ceiling), default=0)
