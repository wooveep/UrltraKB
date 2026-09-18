"""Exact candidate differences used by the one permitted correction recheck."""

from difflib import SequenceMatcher

from openkb.sources import content_id


def changed_claims(previous, title, content):
    before = previous["content"].splitlines(keepends=True)
    after = content.splitlines(keepends=True)
    changes = [
        {
            "before": "".join(before[left:right]),
            "after": "".join(after[start:end]),
            "new_lines": [start + 1, end],
        }
        for operation, left, right, start, end in SequenceMatcher(
            None, before, after, autojunk=False
        ).get_opcodes()
        if operation != "equal"
    ]
    return {
        "previous_candidate": content_id([previous["title"], previous["content"]]),
        "previous_title": previous["title"],
        "title_changed": previous["title"] != title,
        "changes": changes,
        "located_issues": previous.get("issues", []),
        "instruction": (
            "Recheck changed claims and their dependent conditions only. The initial review "
            "already assessed unchanged claims. If a shared prerequisite or title changed, "
            "also check the operations whose meaning it governs. Original evidence remains "
            "authoritative; review feedback is not evidence. An unchanged identified material "
            "error remains blocking. Do not perform a second editorial/full coverage review."
        ),
    }
