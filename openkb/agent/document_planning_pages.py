"""Assign authorized destinations and preserve accepted page increments."""

from __future__ import annotations

import re
import unicodedata

from openkb.agent.document_plan import PagePlan
from openkb.sources import content_id

DEFAULT_PURPOSE = "拟整理该主题，具体内容待原文核对"


def normalized_name(value: str) -> str:
    from openkb.agent.document_planning_semantics import _display_title

    value = _display_title(value)
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def suggestion_key(
    source: str, kind: str, subtype: str | None, name: str, title: str, target: str
) -> str:
    identity = target or (normalized_name(name), normalized_name(title))
    return "page:" + content_id((source, kind, subtype, identity))[:24]


def distinct_scope(previous: PagePlan, purpose: str, hints: list[dict]) -> bool:
    """Keep explicitly exclusive purposes apart; do not infer general semantic aliases."""
    if purpose in {DEFAULT_PURPOSE, previous.purpose} or previous.purpose == DEFAULT_PURPOSE:
        return False
    exclusive = r"\bonly\b|\bexclusively\b|\bfor\b|仅|专用|适用于|面向"
    return bool(
        re.search(exclusive, purpose, re.I)
        and re.search(exclusive, previous.purpose, re.I)
        and hints
        and previous.location_hints
        and not any(hint in previous.location_hints for hint in hints)
    )


def merge_page(previous: PagePlan, incoming: PagePlan, *, add_subject: bool) -> bool:
    """Validate identity before atomically retaining every supported increment."""
    if any(
        getattr(previous, field) != getattr(incoming, field)
        for field in ("key", "kind", "name", "target", "type")
    ):
        raise ValueError("accepted_page_conflict")
    subjects = list(previous.subject_ranges)
    contexts = list(previous.context_ranges)
    notes = list(previous.planning_notes)
    hints = list(previous.location_hints)
    hints.extend(value for value in incoming.location_hints if value not in hints)
    for known, additions in (
        (subjects, incoming.subject_ranges if add_subject else []),
        (contexts, incoming.context_ranges),
    ):
        known.extend(value for value in additions if value not in known)
    notes.extend(value for value in incoming.planning_notes if value not in notes)
    purpose = previous.purpose
    if incoming.purpose != DEFAULT_PURPOSE and incoming.purpose != purpose:
        if purpose == DEFAULT_PURPOSE:
            purpose = incoming.purpose
        elif (note := "补充规划说明：" + incoming.purpose) not in notes:
            notes.append(note)
    changed = (subjects, contexts, notes, purpose, hints) != (
        previous.subject_ranges,
        previous.context_ranges,
        previous.planning_notes,
        previous.purpose,
        previous.location_hints,
    )
    previous.subject_ranges, previous.context_ranges = subjects, contexts
    previous.planning_notes, previous.purpose = notes, purpose
    if changed:
        previous.location_hints = hints
        previous.state, previous.quality, previous.review_receipt = (
            "pending_evidence",
            "planned",
            None,
        )
        previous.scope_resolution = previous.scope_resolution or incoming.scope_resolution
    return changed


def _slug(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).lower()
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:90]
    if not ascii_slug:
        return "topic-" + content_id(title)[:12]
    return ascii_slug + ("-" + content_id(title)[:12] if not normalized.isascii() else "")


def select_page_path(
    *,
    kind: str,
    title: str,
    name: str,
    proposed: str | None,
    existing: set[str],
    allowed: set[str],
    catalog: dict[str, str],
    accepted: dict[str, PagePlan],
    previous: PagePlan | None,
) -> tuple[str, str]:
    """Resolve update permission explicitly; existence only reserves a new path."""
    folder = "concepts" if kind == "concept" else "entities"
    approved = ""
    if proposed:
        if (
            proposed not in existing
            or proposed not in allowed
            or not proposed.startswith(folder + "/")
        ):
            raise ValueError("unknown_target")
        approved = proposed
    elif previous is not None:
        return previous.name, previous.target
    else:
        label = unicodedata.normalize("NFKC", title).casefold().strip()
        matches = [
            path
            for path, text in catalog.items()
            if path.startswith(folder + "/")
            and unicodedata.normalize("NFKC", text).casefold().strip() == label
        ]
        if len(matches) == 1 and matches[0] in allowed and matches[0] in existing:
            approved = matches[0]
    if approved:
        if previous is not None and (previous.name, previous.target) != (approved, approved):
            raise ValueError("accepted_page_conflict")
        if approved in accepted and accepted[approved] is not previous:
            raise ValueError("accepted_page_conflict")
        assert approved in allowed and approved in existing
        return approved, approved
    occupied = existing | accepted.keys()
    slug = _slug(name)
    base = f"{folder}/{slug}"
    if base not in occupied:
        return base, ""
    for attempt in range(len(occupied) + 1):
        suffix = "-new-" + content_id((kind, name))[:10] + (f"-{attempt}" if attempt else "")
        path = f"{folder}/{slug[: 120 - len(suffix)]}{suffix}"
        if path not in occupied:
            return path, ""
    raise ValueError("new_target_conflict")
