"""Locate conversation intent with ConDB, then reread committed user turns."""

from __future__ import annotations

RECALL_VERSION = "ultrakb-user-intent-beam-v2"


def selected_user_excerpts(turns, refs, recent_ids, measure, *, budget):
    """Reread verified committed references and fit all selected locators together."""
    by_id = {t.turn_id: t for t in turns}
    found: list[dict] = []
    selected: list[dict] = []
    seen = set(recent_ids)
    for reference in refs:
        original = by_id.get(reference["turn_id"])
        if (
            original is None
            or original.order != reference["order"]
            or original.content_digest != reference["content_digest"]
        ):
            raise ValueError("Recall candidate no longer matches the committed user turn")
        if original.turn_id in seen:
            continue
        seen.add(original.turn_id)
        selected.append(
            {
                **reference,
                "text": original.user_text,
                "origin": "recalled_user",
                "char_start": 0,
                "char_end": len(original.user_text),
            }
        )
    for index, item in enumerate(selected):
        # Reserve every selected turn's locator and share the remaining text
        # allowance, so one long exchange cannot hide another relevant match.
        frames = [{**turn, "text": ""} for turn in selected[index:]]
        framed_tokens = measure.payload_tokens([*found, *frames])
        available = (budget - framed_tokens) // len(frames)
        if available <= 0:
            break
        item["text"] = measure.prefix(item["text"], available)
        item["char_end"] = len(item["text"])
        # JSON escaping counts toward this turn's share, not just the total.
        # Keep every remaining locator reserved while fitting this excerpt.
        while (
            item["text"]
            and measure.payload_tokens([*found, item, *frames[1:]]) > framed_tokens + available
        ):
            item["text"] = item["text"][:-1]
            item["char_end"] = len(item["text"])
        if item["text"]:
            found.append(item)
    found.sort(key=lambda item: item["order"])
    return found
