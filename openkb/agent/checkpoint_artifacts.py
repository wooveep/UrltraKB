"""Compact, content-free metadata for paginated compilation artifact previews."""

from __future__ import annotations

import json
from typing import Any

from openkb.sources import content_id, valid_id


def _output(value: Any, *, draft: bool) -> tuple[Any, bool]:
    """Return the visible recovery candidate and whether it was adopted."""

    if not draft:
        return value, False
    if not isinstance(value, dict):
        return None, False
    candidate = value.get("output") or value.get("revision")
    # Preview hydration unwraps a recovery record after verifying its digest.
    # Accept that already-unwrapped form too, so the selected durable receipt
    # (rather than a stale index row) remains the source of its review state.
    if candidate is None and isinstance(value.get("page_key"), str):
        candidate = value
    return candidate, isinstance(candidate, dict) and isinstance(candidate.get("page_key"), str)


def _reference(value: Any) -> dict[str, Any] | None:
    """Keep only a portable, body-free original span in the issue index."""

    if not isinstance(value, dict) or not all(
        isinstance(value.get(name), str)
        for name in ("source_id", "version_id", "parse_id", "block_id")
    ):
        return None
    if type(value.get("start")) is not int or type(value.get("end")) is not int:
        return None
    return {
        name: value[name]
        for name in ("source_id", "version_id", "parse_id", "block_id", "start", "end")
    }


def _issue_index(value: dict[str, Any], payload: dict[str, Any], stage: str) -> dict[str, Any]:
    """Summarize only omission identities and their source ranges."""

    if stage == "facts":
        inputs = {
            unit.get("id"): _reference(unit.get("reference"))
            for unit in payload.get("units", [])
            if isinstance(unit, dict) and isinstance(unit.get("id"), str)
        }
        facts = []
        for unit in value.get("units", []):
            if not isinstance(unit, dict) or not (reference := inputs.get(unit.get("id"))):
                continue
            for fact in unit.get("facts", []):
                if isinstance(fact, dict) and isinstance(fact.get("topic"), str):
                    facts.append({"topic": fact["topic"], "reference": reference})
        return {"facts": facts}

    pages = []
    for page in value.get("page_changes", value.get("pages", [])):
        if not isinstance(page, dict):
            continue
        target = page.get("target") or page.get("name")
        ranges = page.get("subject_ranges")
        if (
            isinstance(target, str)
            and target.startswith(("concepts/", "entities/"))
            and isinstance(ranges, list)
        ):
            keys = [
                value
                for value in (page.get("key"), page.get("target_key"), page.get("local_key"))
                if isinstance(value, str) and value
            ]
            pages.append({"target": target, "keys": keys, "ranges": ranges})
    topics = []
    for group in value.get("topics", []):
        if not isinstance(group, dict):
            continue
        kind, name, members = group.get("kind"), group.get("name"), group.get("members")
        if (
            kind in {"concept", "entity"}
            and isinstance(name, str)
            and isinstance(members, list)
            and all(isinstance(member, str) for member in members)
        ):
            topics.append(
                {
                    "target": ("entities/" if kind == "entity" else "concepts/") + name,
                    "topics": members,
                }
            )
    return {"pages": pages, "topics": topics}


def _valid_issue_index(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) == {"facts"}:
        return isinstance(value["facts"], list) and all(
            isinstance(row, dict)
            and isinstance(row.get("topic"), str)
            and _reference(row.get("reference")) == row.get("reference")
            for row in value["facts"]
        )
    if set(value) == {"pages", "topics"}:
        return (
            isinstance(value["pages"], list)
            and isinstance(value["topics"], list)
            and all(
                isinstance(row, dict)
                and isinstance(row.get("target"), str)
                and isinstance(row.get("keys"), list)
                and all(isinstance(key, str) for key in row["keys"])
                and isinstance(row.get("ranges"), list)
                for row in value["pages"]
            )
            and all(
                isinstance(row, dict)
                and isinstance(row.get("target"), str)
                and isinstance(row.get("topics"), list)
                and all(isinstance(topic, str) for topic in row["topics"])
                for row in value["topics"]
            )
        )
    return False


def ledger_progress_preview(value: Any) -> dict[str, Any] | None:
    """Validate the bounded, inspectable part of a resumable planning ledger."""

    if (
        not isinstance(value, dict)
        or value.get("protocol") != "document-plan-ledger-v1"
        or not isinstance(value.get("ledger"), str)
        or len(value["ledger"]) != 64
        or value.get("status") not in {"pending", "accepted"}
        or type(value.get("completed_windows")) is not int
        or type(value.get("window_count")) is not int
        or not 0 <= value["completed_windows"] <= value["window_count"]
        or not isinstance(value.get("accepted_window_ids"), list)
        or len(value["accepted_window_ids"]) != value["completed_windows"]
        or any(not isinstance(item, str) for item in value["accepted_window_ids"])
        or not isinstance(value.get("state_digest"), str)
        or len(value["state_digest"]) != 64
    ):
        return None
    preview = value.get("preview")
    if not isinstance(preview, dict) or set(preview) != {
        "overview",
        "pages",
        "source_only",
        "unresolved",
        "counts",
        "truncated",
    }:
        return None
    overview, counts, truncated = (
        preview["overview"],
        preview["counts"],
        preview["truncated"],
    )
    if (
        not isinstance(overview, dict)
        or not isinstance(overview.get("text"), str)
        or not isinstance(overview.get("ranges"), list)
        or set(counts) != {"pages", "source_only", "unresolved", "open_unresolved"}
        or any(type(count) is not int or count < 0 for count in counts.values())
        or set(truncated) != {"overview", "pages", "source_only", "unresolved"}
        or any(type(flag) is not bool for flag in truncated.values())
        or any(
            not isinstance(preview[name], list)
            or any(not isinstance(item, dict) for item in preview[name])
            for name in ("pages", "source_only", "unresolved")
        )
    ):
        return None
    return preview


def _valid_progress_summary(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"completed_windows", "window_count", "counts", "truncated"}
        and type(value["completed_windows"]) is int
        and type(value["window_count"]) is int
        and isinstance(value["counts"], dict)
        and isinstance(value["truncated"], dict)
    )


def artifact_summary(record: dict[str, Any], *, storage: str) -> dict[str, Any] | None:
    """Extract the minimum metadata needed to paginate without source bodies."""

    identity = record.get("input")
    key = record.get("key")
    if not isinstance(identity, dict) or not isinstance(key, str):
        return None
    value, adopted = _output(record.get("value"), draft=storage == "draft")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    payload = (record.get("contract") or {}).get("payload") or {}
    stage = payload.get("stage")
    if storage == "draft" and stage is None:
        # Recovery drafts are keyed by the generation request but intentionally
        # do not copy its full prompt contract into mutable state.
        stage = "generation"
    metadata = value.get("metadata")
    progress = ledger_progress_preview(value)
    if isinstance(metadata, dict) and metadata.get("protocol") == "document-plan-v1":
        stage = "planning"
    if progress is not None:
        stage = "planning"
    if stage not in {"facts", "planning", "generation", "verification"}:
        return None
    summary: dict[str, Any] = {
        "schema": 1,
        "storage": storage,
        "key": key,
        "stage": stage,
        "source": identity.get("source"),
        "version": identity.get("version"),
        "parse": identity.get("parse"),
        "model": identity.get("model", ""),
        "draft": storage == "draft" and value.get("quality") != "verified",
        "adopted": adopted,
    }
    if progress is not None:
        summary["progress"] = {
            name: value[name] for name in ("completed_windows", "window_count")
        } | {name: progress[name] for name in ("counts", "truncated")}
    if stage == "generation":
        page = payload.get("page") or {}
        page_key = value.get("page_key") if adopted else page.get("key")
        candidate = value.get("candidate") if adopted else value.get("content")
        if isinstance(page_key, str) and isinstance(candidate, str):
            summary["page_key"] = page_key
            # A recovered, adopted candidate already records its stable
            # content identity.  Ordinary generation checkpoints still carry
            # the candidate body, which is deliberately reduced to a digest
            # for the compact index.
            summary["candidate"] = candidate if adopted else content_id(candidate)
        receipt = value.get("review_receipt")
        reviews = receipt.get("reviews") if isinstance(receipt, dict) else None
        if isinstance(reviews, list):
            summary["reviews"] = [
                {key: review.get(key) for key in ("verdict", "candidate", "checkpoint", "result")}
                for review in reviews
                if isinstance(review, dict)
            ]
        fragments = receipt.get("fragments") if isinstance(receipt, dict) else None
        if isinstance(fragments, list) and all(
            isinstance(fragment, dict) and isinstance(fragment.get("candidate"), str)
            for fragment in fragments
        ):
            summary["review_fragments"] = [
                {"candidate": fragment["candidate"]} for fragment in fragments
            ]
    if stage == "verification":
        page = payload.get("page") or {}
        candidate = payload.get("candidate") or {}
        full_candidate = candidate.get("full_content_digest")
        candidate_digest = (
            full_candidate
            if isinstance(full_candidate, str)
            else content_id(candidate["content"])
            if isinstance(candidate.get("content"), str)
            else None
        )
        if (
            value.get("verdict") in {"supported", "advisory"}
            and isinstance(page.get("key"), str)
            and isinstance(candidate_digest, str)
        ):
            summary["verified"] = {
                "page_key": page["key"],
                "candidate": candidate_digest,
                "checkpoint": key,
                "result": content_id(value),
            }
    if stage in {"facts", "planning"} and progress is None:
        summary["issue_index"] = _issue_index(value, payload, stage)
    return summary


def valid_artifact_summary(value: Any) -> bool:
    """Reject malformed index rows before they drive a read-only UI listing."""

    if not isinstance(value, dict) or value.get("schema") != 1:
        return False
    if value.get("storage") not in {"checkpoint", "draft", "plan"}:
        return False
    if value.get("stage") not in {"facts", "planning", "generation", "verification"}:
        return False
    if not all(isinstance(value.get(name), str) for name in ("key", "source", "version", "parse")):
        return False
    try:
        valid_id(value["key"])
        valid_id(value["source"], source=True)
        valid_id(value["version"])
        valid_id(value["parse"])
    except ValueError:
        return False
    verified = value.get("verified")
    return (
        ("model" not in value or isinstance(value["model"], str))
        and ("draft" not in value or type(value["draft"]) is bool)
        and ("adopted" not in value or type(value["adopted"]) is bool)
        and ("page_key" not in value or isinstance(value["page_key"], str))
        and ("candidate" not in value or isinstance(value["candidate"], str))
        and (
            "verified" not in value
            or (
                isinstance(verified, dict)
                and all(
                    isinstance(verified.get(name), str)
                    for name in ("page_key", "candidate", "checkpoint", "result")
                )
            )
        )
        and ("issue_index" not in value or _valid_issue_index(value["issue_index"]))
        and ("progress" not in value or _valid_progress_summary(value["progress"]))
    )
