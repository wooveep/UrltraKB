"""Budget cumulative planning context without changing the source evidence W."""

from __future__ import annotations

import json
import re

from openkb.agent.document_protocol import plan_messages
from openkb.processing import InputTooLarge


def _relevance(text, context):
    text = str(text).casefold()
    words = set(re.findall(r"[a-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", text))
    return sum(word in context for word in words) + 5 * (bool(text) and text in context)


def project_messages(
    evidence,
    state,
    overview,
    target,
    hints,
    catalog,
    entity_types,
    schema,
    settings,
    limits,
    source_conditions,
    subtask,
    recovery,
):
    """Prefer complete context, then relevant details and an honest compact index."""
    context = " ".join(str(row.get("text", "")) for row in evidence.get("blocks", [])).casefold()
    annotations = state.get("suggestion_annotations", {})
    suggestions = state["pages"] + state.get("deferred_suggestions", [])
    ranked = sorted(
        suggestions,
        key=lambda row: -_relevance(
            row["title"]
            + " "
            + row.get("purpose", "")
            + " "
            + json.dumps(row.get("location_hints", []), ensure_ascii=False),
            context,
        ),
    )
    full = [
        {
            "title": row["title"],
            "kind": row.get("kind", "unclassified"),
            "type": row.get("type"),
            "purpose": " ".join(row.get("purpose", "").split()).split("。", 1)[0],
            "aliases": annotations.get(row["key"], {}).get("aliases", [row["title"]]),
        }
        for row in ranked
    ]
    compact = [row["title"] for row in ranked]
    paragraphs = [part for part in overview.strip().split("\n\n") if part.strip()]
    retained = list(range(len(paragraphs)))
    nav = list(hints)
    detailed, shown = len(full), len(full)

    def build():
        carry = {
            "overview": "\n\n".join(paragraphs[i] for i in sorted(retained)),
            "overview_input_clipped": len(retained) != len(paragraphs),
            "processed_overview": (state.get("overview_snapshot") or {}).get("processed", []),
            "pages": full[:detailed],
            "other_titles": compact[detailed:shown],
            "suggestions": {"total": len(full), "shown": shown, "omitted": shown < len(full)},
        }
        messages = plan_messages(
            evidence,
            carry,
            target,
            nav,
            catalog,
            entity_types,
            schema,
            settings.get("language", ""),
            state.get("displayed_targets", []),
            source_conditions=source_conditions,
            subtask=subtask,
            recovery=recovery,
        )
        return messages, {
            "overview_input_clipped": carry["overview_input_clipped"],
            "suggestions": carry["suggestions"],
            "navigation_omitted": len(nav) < len(hints),
        }

    def fits():
        try:
            limits.request(settings["model"], build()[0], {})
            return True
        except InputTooLarge:
            return False

    if fits():
        return build()
    # Reduce unrelated navigation before shortening the cumulative overview.
    nav.sort(key=lambda row: -_relevance(row.get("title", ""), context))
    while nav:
        nav = nav[: len(nav) // 2]
        if fits():
            return build()
    # Whole paragraphs only, with introduction, limits and current themes first.
    priority = sorted(
        retained,
        key=lambda i: (
            -(
                10
                if i < 2
                or re.search(r"限制|未读|前提|场景|limit|unread|prerequisite", paragraphs[i], re.I)
                else 0
            )
            - _relevance(paragraphs[i], context),
            i,
        ),
    )
    while len(retained) > min(2, len(paragraphs)):
        retained.remove(priority.pop())
        if fits():
            return build()
    # Compact the least relevant suggestions first, while retaining every title.
    while detailed:
        detailed //= 2
        if fits():
            return build()
    while shown:
        shown //= 2
        if fits():
            return build()
    while retained:
        retained.remove(priority.pop())
        if fits():
            return build()
    # The caller handles genuinely oversized source W using the existing split path.
    return build()
