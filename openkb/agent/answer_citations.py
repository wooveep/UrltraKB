"""Require source citation targets to have appeared in observed tool evidence."""

import ast
import json
import posixpath
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

_SOURCE = re.compile(r"sources/[^\s)\"'<>\\]+")
_BINDING = re.compile(r"<!-- source-evidence:\s*(\{[^\n]+?\})\s*-->")


class _HtmlLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        field = {"a": "href", "img": "src"}.get(tag)
        if field and (target := dict(attrs).get(field)):
            self.links.append(target)


def _links(text):
    def walk(tokens):
        for token in tokens:
            if token.type in {"link_open", "image"}:
                yield token.attrGet("href" if token.type == "link_open" else "src")
            if token.type in {"html_inline", "html_block"}:
                parser = _HtmlLinks()
                parser.feed(token.content)
                yield from parser.links
            if token.children:
                yield from walk(token.children)

    yield from walk(MarkdownIt("commonmark").enable(["table", "strikethrough"]).parse(text))


def _target(link):
    if not isinstance(link, str):
        return None
    try:
        parts = urlsplit(link)
    except ValueError:
        return (link, "invalid", "")
    if parts.scheme or parts.netloc:
        return None
    path = posixpath.normpath(unquote(parts.path))
    fragment = unquote(parts.fragment)
    if "sources" in path.split("/") or fragment.startswith("block-"):
        # Decode components separately: %23 in a filename is not an anchor.
        return path, parts.query, fragment
    return None


def observation_strings(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            yield value
        else:
            if decoded != value:
                yield from observation_strings(decoded)
    elif isinstance(value, dict):
        for item in value.values():
            yield from observation_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from observation_strings(item)


def source_targets(answer):
    return {target: link for link in _links(answer) if (target := _target(link))}


def invalid_source_targets(result):
    """Check source links only; ordinary external links are outside this contract."""
    answer = getattr(result, "final_output", None)
    if not isinstance(answer, str):
        return []
    from openkb.agent.answer_references import render_references

    _, unresolved = render_references(answer, {}) if "evidence:" in answer else (answer, [])
    targets = source_targets(answer)
    if not targets:
        return unresolved
    observed = set()
    for item in result.to_input_list():
        if item.get("type") == "function_call_output" or item.get("role") == "tool":
            text = "\n".join(observation_strings(item.get("output", item.get("content"))))
            local = {_target(path) for path in _SOURCE.findall(text)}
            # A source reader may return its snapshot path and binding separately.
            # Keep these within one tool observation; never combine loose fields.
            for raw in _BINDING.findall(text):
                try:
                    binding = json.loads(raw)
                except ValueError:
                    try:
                        # Native source pages predate JSON snapshot markers.
                        binding = ast.literal_eval(raw)
                    except (ValueError, SyntaxError):
                        continue
                if not isinstance(binding, dict) or not all(
                    isinstance(binding.get(key), str)
                    and re.fullmatch(r"[A-Za-z0-9_-]+", binding[key])
                    for key in ("source_id", "version_id", "parse_id", "block_id")
                ):
                    continue
                path = f"sources/snapshots/{binding['version_id']}-{binding['parse_id']}.md"
                fragment = f"block-{binding['block_id']}"
                snapshot = _target(path) in local and f'<a id="{fragment}"></a>' in text
                source_page = any(
                    target
                    and target[0].endswith(f"-{binding['source_id']}.md")
                    and posixpath.dirname(target[0]) == "sources"
                    for target in local
                )
                if snapshot or source_page:
                    local.add(_target(f"{path}#{fragment}"))
            observed.update(local)
    return sorted(
        set(unresolved) | {link for target, link in targets.items() if target not in observed}
    )


def require_source_targets(result):
    if invalid_source_targets(result):
        from openkb.processing import ProcessingIncomplete

        raise ProcessingIncomplete("answer_citation_invalid", "answering")
