"""Bounded and terminal projections over the private document-planning ledger."""

import json
from pathlib import Path
from typing import Any

from openkb import frontmatter
from openkb.agent.document_plan import (
    DocumentPlan,
    OverviewPlan,
    PagePlan,
    ResolutionItem,
    SourceOnlyItem,
    UnresolvedItem,
    check_coverage_gaps,
    derive_page_states,
    range_intervals,
)
from openkb.agent.document_window_receipts import target_ranges
from openkb.sources import content_id

_PREVIEW_ROWS = 12
_PREVIEW_RANGES = 12
_PREVIEW_CONTEXTS = 4
_PREVIEW_BASIS_CHARS = 512
_PREVIEW_OVERVIEW_CHARS = 8_000


def catalog_brief(path: Path, folder: str) -> str:
    """Build one legacy-compatible compact catalogue row without a full scan."""

    text = path.read_text(encoding="utf-8")
    metadata = frontmatter.parse(text)
    description = next(
        (
            value.strip()
            for key in ("description", "brief")
            if isinstance(value := metadata.get(key), str) and value.strip()
        ),
        "",
    )
    if not description:
        parts = frontmatter.split(text)
        body = parts[1] if parts is not None else text
        description = body.strip().replace("\n", " ")[:150]
    if folder == "concepts":
        return f"- {path.stem}: {description}" if description else ""
    entity_type = str(metadata.get("type") or "").strip().lower() or "other"
    sources = metadata.get("sources")
    count = len(sources) if isinstance(sources, list) else 0
    suffix = f" — {description}" if description else ""
    return f"- {path.stem} ({entity_type}, {count} sources){suffix}"


def validate_catalog_rows(ledger: Any) -> None:
    """Reject malformed catalogue data before it enters a bounded S projection."""

    for target, brief, digest, baseline in ledger.db.execute(
        "SELECT target, brief, digest, baseline FROM catalog"
    ):
        if (
            not isinstance(target, str)
            or not isinstance(brief, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or content_id(brief) != digest
            or type(baseline) is not int
            or baseline not in {0, 1}
        ):
            raise ValueError("Planning catalogue row is invalid")


def catalog_valid(ledger: Any, wiki: Path, source: Any) -> bool:
    """Verify original entries and every source-owned catalogue addition."""

    marker = f"<!-- openkb-source:{source.source_id} -->"

    def target_path(target: str) -> tuple[Path, str]:
        folder, slash, stem = target.partition("/")
        if folder not in {"concepts", "entities"} or not slash or not stem or "/" in stem:
            raise ValueError("Invalid catalogue target")
        return wiki / folder / f"{stem}.md", folder

    try:
        validate_catalog_rows(ledger)
        for target, digest in ledger.db.execute(
            "SELECT target, digest FROM catalog WHERE baseline = 1 ORDER BY target"
        ):
            path, folder = target_path(target)
            if not path.exists() or content_id(catalog_brief(path, folder)) != digest:
                return False
        additions: list[tuple[str, str, str]] = []
        for folder in ("concepts", "entities"):
            directory = wiki / folder
            if not directory.exists():
                continue
            for path in directory.glob("*.md"):
                target = f"{folder}/{path.stem}"
                if (
                    ledger.db.execute(
                        "SELECT 1 FROM catalog WHERE target = ?", (target,)
                    ).fetchone()
                    is not None
                ):
                    continue
                owner = ledger.db.execute(
                    "SELECT 1 FROM pages WHERE name = ?", (target,)
                ).fetchone()
                if owner is None or marker not in path.read_text(encoding="utf-8"):
                    return False
                brief = catalog_brief(path, folder)
                additions.append((target, brief, content_id(brief)))
        for target, brief, digest in ledger.db.execute(
            "SELECT target, brief, digest FROM catalog WHERE baseline = 0"
        ):
            path, folder = target_path(target)
            owner = ledger.db.execute("SELECT 1 FROM pages WHERE name = ?", (target,)).fetchone()
            if (
                owner is None
                or not path.exists()
                or marker not in path.read_text(encoding="utf-8")
                or (actual_brief := catalog_brief(path, folder)) != brief
                or content_id(actual_brief) != digest
            ):
                return False
        if additions:
            with ledger._transaction():
                for target, brief, digest in additions:
                    ledger.db.execute(
                        "INSERT INTO catalog(target, brief, digest, baseline) VALUES (?, ?, ?, 0)",
                        (target, brief, digest),
                    )
        return True
    except (OSError, ValueError):
        return False


class LedgerKeys:
    def __init__(self, ledger: Any, table: str, *, status: str | None = None):
        self.ledger, self.table, self.status = ledger, table, status

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and self.ledger._has_key(
            self.table, value, status=self.status
        )


class LedgerNames:
    def __init__(self, ledger: Any):
        self.ledger = ledger

    def get(self, name: object, default: Any = None) -> Any:
        if not isinstance(name, str):
            return default
        row = self.ledger.db.execute("SELECT key FROM pages WHERE name = ?", (name,)).fetchone()
        return row[0] if row is not None else default

    def __contains__(self, name: object) -> bool:
        return self.get(name) is not None


def validate_json_rows(ledger: Any, parsed: Any | None = None) -> None:
    """Decode durable rows before a resumed planner trusts its accepted prefix."""

    # Keep validation streaming: recovery must not reconstruct every page or
    # source-only route merely to decide whether it can resume a long document.
    for (value,) in ledger.db.execute("SELECT value FROM meta"):
        json.loads(value)
    validate_catalog_rows(ledger)
    overview = ledger._meta("overview")
    if overview is not None and OverviewPlan.from_dict(overview).status not in {
        "partial",
        "complete",
    }:
        raise ValueError("Planning overview status is invalid")
    for (payload,) in ledger.db.execute("SELECT payload FROM receipts"):
        json.loads(payload)

    for key, name, target, payload in ledger.db.execute(
        "SELECT key, name, target, payload FROM pages"
    ):
        page = PagePlan.from_dict(json.loads(payload))
        if (page.key, page.name, page.target) != (key, name, target):
            raise ValueError("Planning page row key mismatch")
        if (
            page.kind not in {"concept", "entity"}
            or page.quality != "planned"
            or page.review_receipt is not None
            or page.state not in {"ready", "blocked"}
        ):
            raise ValueError("Planning page has non-planner state")
        if parsed is not None:
            for value in page.subject_ranges:
                range_intervals(value, parsed, "recovered page")
            for context in page.necessary_context:
                for value in [*context.get("ranges", []), *context.get("basis_ranges", [])]:
                    range_intervals(value, parsed, "recovered page context")

    for (payload,) in ledger.db.execute("SELECT payload FROM source_only"):
        source_item = SourceOnlyItem.from_dict(json.loads(payload))
        if parsed is not None:
            for value in source_item.ranges:
                range_intervals(value, parsed, "recovered source-only")

    for key, status, payload in ledger.db.execute("SELECT key, status, payload FROM unresolved"):
        item = UnresolvedItem.from_dict(json.loads(payload))
        if (item.key, item.status) != (key, status) or item.status not in {"open", "resolved"}:
            raise ValueError("Planning unresolved row key mismatch")
        if parsed is not None:
            for value in item.location:
                range_intervals(value, parsed, "recovered unresolved")
    for key, payload in ledger.db.execute("SELECT unresolved_key, payload FROM resolutions"):
        if ResolutionItem.from_dict(json.loads(payload)).unresolved_key != key:
            raise ValueError("Planning resolution row key mismatch")
        if ledger.db.execute("SELECT 1 FROM unresolved WHERE key = ?", (key,)).fetchone() is None:
            raise ValueError("Planning resolution has no unresolved item")


def compact_recovery(ledger: Any) -> dict[str, Any]:
    """Return recovery controls without replaying the full accumulated plan."""

    progress = ledger.progress()
    if progress is None:
        return {"protocol": "document-plan-ledger-v1", "ledger": ledger.recovery_key}
    status, windows, completed = progress
    return {
        "protocol": "document-plan-ledger-v1",
        "ledger": ledger.recovery_key,
        "status": status,
        "completed_windows": completed,
        "accepted_window_ids": [receipt["window"] for receipt in ledger.receipts()],
        "window_count": len(windows),
        "state_digest": ledger.state_digest(),
    }


def _ranges(values: list[Any]) -> list[Any]:
    return list(values[:_PREVIEW_RANGES])


def _page_preview(page: PagePlan) -> dict[str, Any]:
    contexts = []
    for context in page.necessary_context[:_PREVIEW_CONTEXTS]:
        basis = context.get("basis", "")
        contexts.append(
            {
                "relation": context.get("relation", ""),
                "basis": basis[:_PREVIEW_BASIS_CHARS],
                "basis_truncated": len(basis) > _PREVIEW_BASIS_CHARS,
                "ranges": _ranges(context.get("ranges", [])),
                "basis_ranges": _ranges(context.get("basis_ranges", [])),
            }
        )
    return {
        "key": page.key,
        "kind": page.kind,
        "type": page.type,
        "name": page.name,
        "title": page.title,
        "purpose": page.purpose,
        "target": page.target,
        "state": page.state,
        "quality": page.quality,
        "subject_ranges": _ranges(page.subject_ranges),
        "subject_range_count": len(page.subject_ranges),
        "necessary_context": contexts,
        "necessary_context_count": len(page.necessary_context),
    }


def progress_preview(ledger: Any, *, limit: int = _PREVIEW_ROWS) -> dict[str, Any]:
    """Hydrate a small inspectable prefix without loading the whole ledger."""

    if type(limit) is not int or limit < 1:
        raise ValueError("Planning preview limit is invalid")
    value = compact_recovery(ledger)
    overview = ledger.overview().to_dict()
    overview["range_count"] = len(overview["ranges"])
    overview["ranges"] = _ranges(overview["ranges"])
    overview["ranges_truncated"] = overview["range_count"] > len(overview["ranges"])
    overview["text_truncated"] = len(overview["text"]) > _PREVIEW_OVERVIEW_CHARS
    overview["text"] = overview["text"][:_PREVIEW_OVERVIEW_CHARS]
    pages = [
        _page_preview(PagePlan.from_dict(json.loads(payload)))
        for (payload,) in ledger.db.execute(
            "SELECT payload FROM pages ORDER BY rowid LIMIT ?", (limit,)
        )
    ]
    source_only = [
        {
            "reason": item.reason,
            "ranges": _ranges(item.ranges),
            "range_count": len(item.ranges),
        }
        for (payload,) in ledger.db.execute(
            "SELECT payload FROM source_only ORDER BY key LIMIT ?", (limit,)
        )
        for item in [SourceOnlyItem.from_dict(json.loads(payload))]
    ]
    unresolved = [
        {
            "key": item.key,
            "problem_type": item.problem_type,
            "missing_target": item.missing_target,
            "affected_pages": item.affected_pages,
            "blocking": item.blocking,
            "reason": item.reason,
            "status": item.status,
            "location": _ranges(item.location),
            "location_count": len(item.location),
        }
        for (payload,) in ledger.db.execute(
            "SELECT payload FROM unresolved ORDER BY rowid LIMIT ?", (limit,)
        )
        for item in [UnresolvedItem.from_dict(json.loads(payload))]
    ]
    counts = {
        "pages": ledger.page_count(),
        "source_only": int(ledger.db.execute("SELECT COUNT(*) FROM source_only").fetchone()[0]),
        "unresolved": int(ledger.db.execute("SELECT COUNT(*) FROM unresolved").fetchone()[0]),
        "open_unresolved": ledger.open_unresolved_count(),
    }
    value["preview"] = {
        "overview": overview,
        "pages": pages,
        "source_only": source_only,
        "unresolved": unresolved,
        "counts": counts,
        "truncated": {
            "overview": overview["text_truncated"] or overview["ranges_truncated"],
            "pages": counts["pages"] > len(pages),
            "source_only": counts["source_only"] > len(source_only),
            "unresolved": counts["unresolved"] > len(unresolved),
        },
    }
    return value


def materialize(ledger: Any):
    """Create the complete formal plan only at terminal hand-off."""

    pages = [
        PagePlan.from_dict(json.loads(payload))
        for (payload,) in ledger.db.execute("SELECT payload FROM pages ORDER BY rowid")
    ]
    source_only = [
        SourceOnlyItem.from_dict(json.loads(payload))
        for (payload,) in ledger.db.execute("SELECT payload FROM source_only ORDER BY key")
    ]
    unresolved = [
        UnresolvedItem.from_dict(json.loads(payload))
        for (payload,) in ledger.db.execute("SELECT payload FROM unresolved ORDER BY rowid")
    ]
    resolutions = [
        ResolutionItem.from_dict(json.loads(payload))
        for (payload,) in ledger.db.execute(
            "SELECT payload FROM resolutions ORDER BY unresolved_key"
        )
    ]
    derive_page_states(pages, unresolved)
    return ledger.overview(), pages, source_only, unresolved, resolutions


def terminal_coverage_valid(ledger: Any, windows: list[dict[str, Any]], parsed: Any) -> bool:
    """Recheck the terminal W/T schedule against its materialized routes."""

    try:
        overview, pages, source_only, unresolved, resolutions = materialize(ledger)
        plan = DocumentPlan(
            overview=overview,
            pages=pages,
            source_only=source_only,
            unresolved=unresolved,
            resolutions=resolutions,
        )
        required_ranges = [item for window in windows for item in target_ranges(window)]
        return not check_coverage_gaps(parsed, plan, required_ranges=required_ranges)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def final_catalog_targets(ledger: Any) -> set[str]:
    return {
        target
        for (target,) in ledger.db.execute(
            "SELECT target FROM catalog WHERE baseline = 1 ORDER BY target"
        )
    }


def final_catalog_metadata(ledger: Any) -> dict[str, Any]:
    entries = list(
        ledger.db.execute("SELECT target, brief FROM catalog WHERE baseline = 1 ORDER BY target")
    )
    return {
        "catalog_targets": [target for target, _ in entries],
        "catalog_entry_snapshot": {target: content_id(brief) for target, brief in entries},
        "catalog_snapshot": ledger._catalog_snapshot(),
        "catalog_ledger": ledger.catalog_manifest(),
    }
