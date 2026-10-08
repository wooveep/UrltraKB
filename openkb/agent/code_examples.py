"""Identify structured examples without treating every colon as a field.

Only parse data. Shell snippets are tokenized to locate curl's literal request
body; commands, file references, and YAML constructors are never executed.
"""

import json
import re
import shlex
from dataclasses import dataclass

import yaml
from markdown_it import MarkdownIt
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from openkb.agent.page_response import PageResponseError

MAX_EXAMPLE_CHARS = 64000
MAX_EXAMPLE_NODES = 4096
MAX_EXAMPLE_DEPTH = 32


@dataclass(frozen=True)
class CodeExample:
    content: str
    language: str = ""


def code_examples(markdown: str) -> list[CodeExample]:
    examples = []
    for token in MarkdownIt("commonmark").parse(markdown):
        if token.type in {"fence", "code_block"}:
            examples.append(CodeExample(token.content, token.info.split()[0] if token.info else ""))
        for child in token.children or []:
            if child.type == "code_inline" and ":" in child.content:
                examples.append(CodeExample(child.content))
    return examples


def example_keys(example: CodeExample) -> set[str]:
    body = example.content.strip()
    language = example.language.casefold()
    # A URI is a scalar even when a text extractor split the source scheme.
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$", body):
        return set()
    if language in {"sh", "shell", "bash", "console"} or body.startswith("curl "):
        return _curl_keys(body)
    if language == "json" or body.startswith(("{", "[")):
        return _json_keys(body)
    if language in {"yaml", "yml"} or (not language and re.match(r"^[\w.-]+:\s+", body)):
        return _yaml_keys(body)
    return set()


def _check_size(body: str) -> None:
    if len(body) > MAX_EXAMPLE_CHARS:
        raise PageResponseError("page_example_budget", "Structured example exceeds parsing budget")


def _json_keys(body: str) -> set[str]:
    _check_size(body)
    try:
        value = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise PageResponseError("page_invalid_example", "Malformed JSON example") from exc
    keys: set[str] = set()
    pending = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        _check_traversal(visited, depth)
        if isinstance(item, dict):
            keys.update(item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return keys


def _yaml_keys(body: str) -> set[str]:
    _check_size(body)
    try:
        root = yaml.compose(body, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise PageResponseError("page_invalid_example", "Malformed YAML example") from exc
    keys: set[str] = set()
    pending: list[tuple[Node | None, int]] = [(root, 0)]
    seen: set[int] = set()
    while pending:
        node, depth = pending.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        _check_traversal(len(seen), depth)
        if isinstance(node, MappingNode):
            for key, value in node.value:
                if isinstance(key, ScalarNode):
                    keys.add(key.value)
                else:
                    raise PageResponseError("page_invalid_example", "Complex YAML mapping key")
                pending.append((value, depth + 1))
        elif isinstance(node, SequenceNode):
            pending.extend((child, depth + 1) for child in node.value)
    return keys


def _check_traversal(nodes: int, depth: int) -> None:
    if nodes > MAX_EXAMPLE_NODES or depth > MAX_EXAMPLE_DEPTH:
        raise PageResponseError("page_example_budget", "Structured example exceeds parsing budget")


def _curl_keys(body: str) -> set[str]:
    _check_size(body)
    try:
        words = shlex.split(body.replace("\\\n", ""))
    except ValueError as exc:
        raise PageResponseError("page_invalid_example", "Malformed shell example") from exc
    if not words or words[0] != "curl":
        return set()
    keys: set[str] = set()
    flags = {"-d", "--data", "--data-raw", "--data-binary", "--json"}
    for index, word in enumerate(words):
        payload = ""
        if word in flags and index + 1 < len(words):
            payload = words[index + 1]
        elif word.partition("=")[0] in flags and "=" in word:
            payload = word.partition("=")[2]
        elif word.startswith("-d") and len(word) > 2:
            payload = word[2:]
        if payload.lstrip().startswith(("{", "[")):
            keys.update(_json_keys(payload))
    return keys
