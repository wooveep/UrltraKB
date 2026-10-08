"""Resolve retry identities against frozen originals, not model wording."""

import json
import re

from openkb.state import HashRegistry


def rule_fragments(text):
    return [re.sub(r"\s+", " ", rule).strip() for rule in re.split(r"[。！？;；]|\.\s+", text)]


def original_rules(view, path):
    target = (view.scope.wiki_dir / path).resolve()
    if (
        not target.is_relative_to(view.scope.wiki_dir)
        or not target.is_file()
        or HashRegistry.hash_file(target) != view.files[path]
    ):
        return ()
    text = target.read_text("utf-8")
    if path.endswith(".json"):
        raw = json.loads(text)
        text = (
            "\n".join(page.get("content", "") for page in raw)
            if isinstance(raw, list)
            else raw.get("text", "")
        )
    return tuple(rule_fragments(text))


def rule_anchor(rules, content, subject):
    # Matching against the full frozen rule keeps a sliced read, another tool
    # locator, or a different subject anchor from creating a fresh retry budget.
    subject = " ".join(subject.split())
    fragments = [part for part in rule_fragments(content) if subject and subject in part]
    for fragment in fragments:
        tail = fragment[fragment.index(subject) :].rstrip(".")
        for index, rule in enumerate(rules):
            if subject in rule and (tail in rule or rule.rstrip(".") in fragment):
                return index
    # Unknown/ambiguous anchors share a conservative bucket for this original;
    # invalid subject spelling never buys an additional retry.
    return -1
