"""Disk-backed, lazily projected planning state; W/T views are loaded on demand."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from openkb.agent.document_plan import (
    OverviewPlan,
    PagePlan,
    ResolutionItem,
    SourceOnlyItem,
    UnresolvedItem,
)
from openkb.agent.document_planning_ledger_integrity import (
    accepted_proofs_valid as _accepted_proofs_valid,
)
from openkb.agent.document_planning_ledger_integrity import (
    catalog_baseline_proof_valid as _catalog_baseline_proof_valid,
)
from openkb.agent.document_planning_ledger_integrity import (
    save_accepted_proof as _save_accepted_proof,
)
from openkb.agent.document_planning_ledger_integrity import (
    save_catalog_baseline_proof as _save_catalog_baseline_proof,
)
from openkb.agent.document_planning_ledger_views import (
    LedgerKeys,
    LedgerNames,
)
from openkb.agent.document_planning_ledger_views import (
    catalog_brief as _brief,
)
from openkb.agent.document_planning_ledger_views import (
    catalog_valid as _catalog_valid,
)
from openkb.agent.document_planning_ledger_views import (
    compact_recovery as _compact_recovery,
)
from openkb.agent.document_planning_ledger_views import (
    final_catalog_metadata as _final_catalog_metadata,
)
from openkb.agent.document_planning_ledger_views import (
    final_catalog_targets as _final_catalog_targets,
)
from openkb.agent.document_planning_ledger_views import (
    materialize as _materialize,
)
from openkb.agent.document_planning_ledger_views import (
    progress_preview as _progress_preview,
)
from openkb.agent.document_planning_ledger_views import (
    terminal_coverage_valid as _terminal_coverage_valid,
)
from openkb.agent.document_planning_ledger_views import (
    validate_json_rows as _validate_ledger_rows,
)
from openkb.agent.document_planning_projection import _WIKILINK_TARGET, _mentions, touches_target
from openkb.sources import content_id

_VIEW_CANDIDATES = 32


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class DocumentPlanningLedger:
    """Append accepted planning deltas atomically and project bounded views on demand."""

    def __init__(self, checkpoints: Any, recovery_key: str):
        self.checkpoints = checkpoints
        directory = checkpoints.store.owned_path(checkpoints.root / "planning-ledgers")
        directory.mkdir(parents=True, exist_ok=True)
        self.path = checkpoints.store.owned_path(directory / f"{recovery_key}.sqlite3")
        self.recovery_key = recovery_key
        self.db = self._open()

    def _open(self):
        try:
            db = sqlite3.connect(self.path)
            db.execute("PRAGMA foreign_keys = ON")
            self.db = db
            self._schema()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Planning ledger integrity check failed")
            self._validate_json_rows()
            return db
        except sqlite3.DatabaseError:
            if "db" in locals():
                db.close()
            self._quarantine()
            db = sqlite3.connect(self.path)
            db.execute("PRAGMA foreign_keys = ON")
            self.db = db
            self._schema()
            return db

    def _validate_json_rows(self, parsed: Any | None = None) -> None:
        """Turn malformed durable rows into the normal reset/cache-miss path."""

        try:
            _validate_ledger_rows(self, parsed)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise sqlite3.DatabaseError("Planning ledger contains invalid durable rows") from exc

    def _quarantine(self) -> None:
        if not self.path.exists():
            return
        directory = self.path.parent / "quarantine"
        directory.mkdir(exist_ok=True)
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()[:16]
        for suffix in ("", "-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            if path.exists():
                path.replace(directory / f"{self.recovery_key}-{digest}{suffix}.sqlite3")

    def close(self) -> None:
        self.db.close()

    def _schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS catalog (
                target TEXT PRIMARY KEY,
                brief TEXT NOT NULL,
                digest TEXT NOT NULL,
                baseline INTEGER NOT NULL CHECK(baseline IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS pages (
                key TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                target TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_only (
                key TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS unresolved (
                key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resolutions (
                unresolved_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
                sequence INTEGER PRIMARY KEY,
                payload TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def _meta(self, name: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row is not None else default

    def _set_meta(self, name: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO meta(name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, _json(value)),
        )

    def _catalog_snapshot(self, *, baseline_only: bool = True) -> str:
        where = " WHERE baseline = 1" if baseline_only else ""
        digest = hashlib.sha256()
        for target, brief, value in self.db.execute(
            "SELECT target, brief, digest FROM catalog" + where + " ORDER BY target"
        ):
            digest.update(_json([target, brief, value]).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _state_digest(self) -> str:
        digest = hashlib.sha256()
        for table, columns in (
            ("pages", "key, name, target, payload"),
            ("source_only", "key, payload"),
            ("unresolved", "key, status, payload"),
            ("resolutions", "unresolved_key, payload"),
            ("receipts", "sequence, payload"),
        ):
            digest.update(table.encode("ascii") + b"\n")
            for row in self.db.execute(f"SELECT {columns} FROM {table} ORDER BY 1"):
                digest.update(_json(list(row)).encode("utf-8"))
                digest.update(b"\n")
        # These fields decide which windows may be skipped on Continue.  They
        # must be covered by the same integrity receipt as their rows.
        for name in ("overview", "status", "windows", "completed"):
            digest.update(name.encode("ascii") + b"\n")
            digest.update(_json(self._meta(name)).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _refresh_integrity(self) -> None:
        self._set_meta("state_digest", self._state_digest())

    def _transaction(self):
        class Transaction:
            def __init__(self, ledger: DocumentPlanningLedger):
                self.ledger = ledger

            def __enter__(self):
                self.ledger.db.execute("BEGIN IMMEDIATE")
                return self.ledger

            def __exit__(self, exc_type, _exc, _traceback):
                if exc_type is None:
                    self.ledger.db.commit()
                else:
                    self.ledger.db.rollback()

        return Transaction(self)

    @property
    def initialized(self) -> bool:
        return self._meta("identity") is not None

    def baseline_valid(self) -> bool:
        """Whether the external proof still anchors this initial catalogue."""

        return _catalog_baseline_proof_valid(self)

    def initialize(self, *, wiki: Path, source: Any, parsed: Any) -> None:
        """Create an immutable baseline catalogue one row at a time."""

        identity = {
            "recovery_key": self.recovery_key,
            "source_id": source.source_id,
            "version_id": source.id,
            "parse_id": parsed.id,
        }
        stored = self._meta("identity")
        if stored is not None:
            if stored != identity:
                raise ValueError("Recovered DocumentPlan identity mismatch")
            return
        with self._transaction():
            self._set_meta("identity", identity)
            for folder in ("concepts", "entities"):
                directory = wiki / folder
                if not directory.exists():
                    continue
                for path in directory.glob("*.md"):
                    target = f"{folder}/{path.stem}"
                    brief = _brief(path, folder)
                    self.db.execute(
                        "INSERT INTO catalog(target, brief, digest, baseline) VALUES (?, ?, ?, 1)",
                        (target, brief, content_id(brief)),
                    )
            self._set_meta("catalog_snapshot", self._catalog_snapshot())
            self._set_meta("catalog_count", self.catalog_count())
            self._set_meta("overview", OverviewPlan(status="partial").to_dict())
            self._refresh_integrity()
        _save_catalog_baseline_proof(self)

    def reset(self) -> None:
        """Discard an invalid recovery prefix before rebuilding its baseline."""

        with self._transaction():
            for table in (
                "meta",
                "catalog",
                "pages",
                "source_only",
                "unresolved",
                "resolutions",
                "receipts",
            ):
                self.db.execute(f"DELETE FROM {table}")

    def catalog_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM catalog").fetchone()[0])

    def catalog_manifest(self) -> dict[str, Any]:
        return {
            "snapshot": self._meta("catalog_snapshot"),
            "count": self._meta("catalog_count", 0),
            "ledger": self.recovery_key,
        }

    def bind_metadata(self, metadata: dict[str, Any]) -> None:
        """Bind immutable plan inputs after the catalogue baseline exists."""

        stored = self._meta("planning_metadata")
        if stored is None:
            with self._transaction():
                self._set_meta("planning_metadata", metadata)
            return
        if stored != metadata:
            raise ValueError("Recovered DocumentPlan identity mismatch")

    def verify_catalog(self, *, wiki: Path, source: Any) -> bool:
        """Reject changed baseline pages; permit only this source's published additions."""
        return _catalog_valid(self, wiki, source)

    def overview(self) -> OverviewPlan:
        return OverviewPlan.from_dict(self._meta("overview", {}))

    def state_digest(self) -> str:
        value = self._meta("state_digest")
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("Planning ledger integrity is invalid")
        return value

    def _has_key(self, table: str, key: str, *, status: str | None = None) -> bool:
        if table == "catalog":
            row = self.db.execute("SELECT 1 FROM catalog WHERE target = ?", (key,)).fetchone()
        elif status is None:
            row = self.db.execute(f"SELECT 1 FROM {table} WHERE key = ?", (key,)).fetchone()
        else:
            row = self.db.execute(
                f"SELECT 1 FROM {table} WHERE key = ? AND status = ?", (key, status)
            ).fetchone()
        return row is not None

    def page_keys(self) -> LedgerKeys:
        return LedgerKeys(self, "pages")

    def page_names(self) -> LedgerNames:
        return LedgerNames(self)

    def unresolved_keys(self, *, status: str | None = None) -> LedgerKeys:
        return LedgerKeys(self, "unresolved", status=status)

    def catalog_targets(self) -> LedgerKeys:
        return LedgerKeys(self, "catalog")

    def page_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0])

    def open_unresolved_count(self) -> int:
        return int(
            self.db.execute("SELECT COUNT(*) FROM unresolved WHERE status = 'open'").fetchone()[0]
        )

    def _page(self, key: str) -> PagePlan | None:
        row = self.db.execute("SELECT payload FROM pages WHERE key = ?", (key,)).fetchone()
        return PagePlan.from_dict(json.loads(row[0])) if row is not None else None

    def _unresolved(self, key: str) -> UnresolvedItem | None:
        row = self.db.execute("SELECT payload FROM unresolved WHERE key = ?", (key,)).fetchone()
        return UnresolvedItem.from_dict(json.loads(row[0])) if row is not None else None

    @staticmethod
    def _references(evidence: dict[str, Any]) -> tuple[str, set[str]]:
        text = "\n".join(
            block.get("text", "")
            for block in evidence.get("blocks", [])
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
        return text, {
            match.group(1).strip()
            for match in _WIKILINK_TARGET.finditer(text)
            if match.group(1).strip()
        }

    def relevant_keys(
        self,
        evidence: dict[str, Any],
        target_start: int,
        target_end: int,
        *,
        page_keys: Iterable[str] = (),
        catalog_targets: Iterable[str] = (),
        unresolved_keys: Iterable[str] = (),
    ) -> dict[str, set[str]]:
        """Find dependencies by streaming rows instead of rebuilding cumulative S."""

        text, references = self._references(evidence)
        relevant_pages = set(page_keys)
        relevant_catalog = set(catalog_targets)
        relevant_unresolved = set(unresolved_keys)
        for key, payload in self.db.execute("SELECT key, payload FROM pages ORDER BY rowid"):
            page = PagePlan.from_dict(json.loads(payload))
            aliases = {page.key, page.name, page.title, page.target} - {""}
            if aliases & references or touches_target(
                page.subject_ranges, target_start, target_end
            ):
                relevant_pages.add(key)
                if page.target:
                    relevant_catalog.add(page.target)
        changed = True
        while changed:
            changed = False
            for key, payload in self.db.execute(
                "SELECT key, payload FROM unresolved WHERE status = 'open' ORDER BY key"
            ):
                item = UnresolvedItem.from_dict(json.loads(payload))
                named_page = any(
                    (affected_page := self._page(page_key)) is not None
                    and any(
                        _mentions(text, alias)
                        for alias in (
                            affected_page.key,
                            affected_page.name,
                            affected_page.title,
                            affected_page.target,
                        )
                    )
                    for page_key in item.affected_pages
                )
                if (
                    key in relevant_unresolved
                    or touches_target(item.location, target_start, target_end)
                    or set(item.affected_pages) & relevant_pages
                    or key in references
                    or item.missing_target in references
                    or _mentions(text, item.missing_target)
                    or named_page
                ):
                    before = len(relevant_pages), len(relevant_unresolved)
                    relevant_unresolved.add(key)
                    relevant_pages.update(item.affected_pages)
                    changed = before != (len(relevant_pages), len(relevant_unresolved))
        for key in relevant_pages:
            if (catalog_page := self._page(key)) is not None and catalog_page.target:
                relevant_catalog.add(catalog_page.target)
        return {
            "page_keys": relevant_pages,
            "unresolved_keys": relevant_unresolved,
            "catalog_targets": relevant_catalog,
        }

    @staticmethod
    def _page_row(page: PagePlan) -> dict[str, Any]:
        return {
            "key": page.key,
            "name": page.name,
            "title": page.title,
            "kind": page.kind,
            "type": page.type,
            "purpose": page.purpose,
            "target": page.target,
        }

    def projected_carry(
        self, relevant: dict[str, set[str]], target_start: int, target_end: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Load required identities and a bounded amount of nearby state."""

        pages, seen = [], set()
        for key in sorted(relevant["page_keys"]):
            if (page := self._page(key)) is not None:
                pages.append(self._page_row(page))
                seen.add(key)
        for key, payload in self.db.execute("SELECT key, payload FROM pages ORDER BY key"):
            if key in seen:
                continue
            page = PagePlan.from_dict(json.loads(payload))
            if touches_target(page.subject_ranges, target_start, target_end):
                pages.append(self._page_row(page))
                seen.add(key)
        if len(pages) < _VIEW_CANDIDATES:
            for key, payload in self.db.execute("SELECT key, payload FROM pages ORDER BY rowid"):
                if key in seen:
                    continue
                pages.append(self._page_row(PagePlan.from_dict(json.loads(payload))))
                seen.add(key)
                if len(pages) >= _VIEW_CANDIDATES:
                    break

        unresolved, seen = [], set()
        for key in sorted(relevant["unresolved_keys"]):
            if (item := self._unresolved(key)) is not None and item.status == "open":
                unresolved.append(item.to_dict())
                seen.add(key)
        for key, payload in self.db.execute(
            "SELECT key, payload FROM unresolved WHERE status = 'open' ORDER BY rowid"
        ):
            if key in seen:
                continue
            item = UnresolvedItem.from_dict(json.loads(payload))
            if touches_target(item.location, target_start, target_end):
                unresolved.append(item.to_dict())
                seen.add(key)
        if len(unresolved) < _VIEW_CANDIDATES:
            for key, payload in self.db.execute(
                "SELECT key, payload FROM unresolved WHERE status = 'open' ORDER BY rowid"
            ):
                if key in seen:
                    continue
                unresolved.append(UnresolvedItem.from_dict(json.loads(payload)).to_dict())
                seen.add(key)
                if len(unresolved) >= _VIEW_CANDIDATES:
                    break
        return pages, unresolved

    def ranked_catalog(self, terms: set[str], required: set[str]) -> list[tuple[str, str]]:
        """Read direct requirements plus a bounded lexical catalogue sample."""

        result, seen = [], set()
        for target in sorted(required):
            row = self.db.execute(
                "SELECT brief FROM catalog WHERE target = ?", (target,)
            ).fetchone()
            if row is not None:
                result.append((target, row[0]))
                seen.add(target)
        matches: list[tuple[str, str]] = []
        others: list[tuple[str, str]] = []
        for target, brief in self.db.execute("SELECT target, brief FROM catalog ORDER BY target"):
            if target in seen:
                continue
            bucket = (
                matches if any(term in (target + " " + brief).lower() for term in terms) else others
            )
            if len(bucket) < _VIEW_CANDIDATES:
                bucket.append((target, brief))
        return [*result, *matches, *others]

    def _store_page(self, page: PagePlan) -> None:
        self.db.execute(
            "INSERT INTO pages(key, name, target, payload) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET name = excluded.name, target = excluded.target, "
            "payload = excluded.payload",
            (page.key, page.name, page.target, _json(page.to_dict())),
        )

    def _set_page_state(self, key: str) -> None:
        page = self._page(key)
        if page is None:
            return
        blocked = False
        for (payload,) in self.db.execute(
            "SELECT payload FROM unresolved WHERE status = 'open' AND key != ''"
        ):
            item = UnresolvedItem.from_dict(json.loads(payload))
            if item.blocking and key in item.affected_pages:
                blocked = True
                break
        page.state = "blocked" if blocked else "ready"
        self._store_page(page)

    def apply_accepted(
        self,
        decoded: dict[str, Any],
        receipt: dict[str, Any],
        *,
        final_window: bool,
        windows: list[dict[str, Any]],
        completed: int,
    ) -> OverviewPlan:
        """Atomically apply one validated delta, receipt, and resumable prefix."""

        with self._transaction():
            overview = self.overview()
            overview.text = decoded["overview"]["text"]
            for value in decoded["overview"]["ranges"]:
                if value not in overview.ranges:
                    overview.ranges.append(value)
            for limitation in decoded["overview"]["limitations"]:
                if limitation not in overview.limitations:
                    overview.limitations.append(limitation)
            overview.status = "complete" if final_window else "partial"
            self._set_meta("overview", overview.to_dict())

            affected: set[str] = set()
            for change in decoded["page_changes"]:
                existing = self._page(change["target_key"])
                if existing is not None:
                    for value in change["subject_ranges"]:
                        if value not in existing.subject_ranges:
                            existing.subject_ranges.append(value)
                    for context in change["necessary_context"]:
                        if context not in existing.necessary_context:
                            existing.necessary_context.append(context)
                    if change.get("purpose") and not existing.purpose:
                        existing.purpose = change["purpose"]
                    self._store_page(existing)
                    affected.add(existing.key)
                    continue
                page = PagePlan(
                    key=change["target_key"],
                    kind=change["kind"],
                    type=change.get("type"),
                    name=change["name"],
                    title=change["title"],
                    purpose=change.get("purpose", ""),
                    target=change.get("target", ""),
                    subject_ranges=list(change["subject_ranges"]),
                    necessary_context=list(change["necessary_context"]),
                    state="ready",
                    quality="planned",
                    local_key=change.get("local_key"),
                )
                self._store_page(page)
                affected.add(page.key)

            for item in decoded["source_only"]:
                source_only = SourceOnlyItem.from_dict(item)
                key = content_id(source_only.to_dict())
                self.db.execute(
                    "INSERT OR IGNORE INTO source_only(key, payload) VALUES (?, ?)",
                    (key, _json(source_only.to_dict())),
                )
            for item in decoded["unresolved"]:
                unresolved = UnresolvedItem.from_dict(item)
                self.db.execute(
                    "INSERT INTO unresolved(key, status, payload) VALUES (?, ?, ?)",
                    (unresolved.key, unresolved.status, _json(unresolved.to_dict())),
                )
                affected.update(unresolved.affected_pages)
            for value in decoded["resolutions"]:
                resolution = ResolutionItem.from_dict(value)
                issue = self._unresolved(resolution.unresolved_key)
                if issue is None:
                    raise ValueError("Resolution has no durable unresolved item")
                issue.status = "resolved"
                self.db.execute(
                    "UPDATE unresolved SET status = ?, payload = ? WHERE key = ?",
                    (issue.status, _json(issue.to_dict()), issue.key),
                )
                for page_key in issue.affected_pages:
                    affected.add(page_key)
                self.db.execute(
                    "INSERT INTO resolutions(unresolved_key, payload) VALUES (?, ?)",
                    (resolution.unresolved_key, _json(resolution.to_dict())),
                )
            for key in affected:
                self._set_page_state(key)
            self.db.execute(
                "INSERT INTO receipts(sequence, payload) VALUES (?, ?)",
                (completed, _json(receipt)),
            )
            self._set_meta("windows", windows)
            self._set_meta("completed", completed)
            self._set_meta("status", "pending")
            self._refresh_integrity()
            # Save this independently durable receipt while the ledger change
            # remains transactional.  A cancellation cannot leave a prefix
            # that looks accepted without a proof of its post-delta contents.
            _save_accepted_proof(self, receipt, completed)
        return overview

    def replace_schedule(self, windows: list[dict[str, Any]], completed: int) -> None:
        """Atomically retain a T/W subdivision before its first child runs."""

        if type(completed) is not int or not 0 <= completed <= len(windows):
            raise ValueError("Invalid replacement planning schedule")
        prior = self.progress()
        if prior is not None and (
            prior[0] != "pending"
            or prior[2] != completed
            or prior[1][:completed] != windows[:completed]
        ):
            raise ValueError("Replacement planning schedule changes accepted work")
        with self._transaction():
            self._set_meta("windows", windows)
            self._set_meta("completed", completed)
            self._set_meta("status", "pending")
            self._refresh_integrity()

    def receipts(self) -> list[dict[str, Any]]:
        return [
            json.loads(payload)
            for _, payload in self.db.execute(
                "SELECT sequence, payload FROM receipts ORDER BY sequence"
            )
        ]

    def receipt(self, sequence: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT payload FROM receipts WHERE sequence = ?", (sequence,)
        ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def progress(self) -> tuple[str, list[dict[str, Any]], int] | None:
        status, windows, completed = (
            self._meta("status"),
            self._meta("windows"),
            self._meta("completed"),
        )
        if status is None and windows is None and completed is None:
            return None
        if (
            status not in {"pending", "accepted"}
            or not isinstance(windows, list)
            or type(completed) is not int
        ):
            raise ValueError("Planning ledger progress is invalid")
        return status, windows, completed

    def recovery_valid(
        self,
        windows: list[dict[str, Any]],
        completed: int,
        dispatch_lookup: Any,
        *,
        parsed: Any,
        source: Any,
        base_windows: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Validate a persisted prefix without loading every ledger value at once."""

        from openkb.agent.document_planning_support import valid_window_schedule
        from openkb.agent.document_window_receipts import (
            accepted_receipts_match_dispatch,
            valid_accepted_receipts,
        )

        try:
            self._validate_json_rows(parsed)
        except sqlite3.DatabaseError:
            return False
        if not valid_window_schedule(windows, parsed, source=source, base_schedule=base_windows):
            return False
        if self._state_digest() != self._meta("state_digest"):
            return False
        if completed < 0 or completed > len(windows):
            return False
        if self._meta("status") == "accepted" and completed != len(windows):
            return False
        if int(self.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]) != completed:
            return False
        receipts = []
        for index, window in enumerate(windows[:completed], start=1):
            receipt = self.receipt(index)
            if receipt is None:
                return False
            if not valid_accepted_receipts([receipt], [window], 1):
                return False
            if not accepted_receipts_match_dispatch([receipt], dispatch_lookup):
                return False
            receipts.append(receipt)
        if not _accepted_proofs_valid(self, receipts):
            return False
        if self._meta("status") == "accepted":
            if not _terminal_coverage_valid(self, windows, parsed):
                return False
        return True

    def mark_accepted(self, windows: list[dict[str, Any]]) -> None:
        with self._transaction():
            # A terminal ledger means every target has been classified.  This
            # also covers an empty/readability-free source, which has no
            # window through ``apply_accepted`` to update the durable overview.
            overview = self.overview()
            overview.status = "complete"
            self._set_meta("overview", overview.to_dict())
            self._set_meta("windows", windows)
            self._set_meta("completed", len(windows))
            self._set_meta("status", "accepted")
            self._refresh_integrity()

    def compact_recovery(self) -> dict[str, Any]:
        return _compact_recovery(self)

    def progress_preview(self) -> dict[str, Any]:
        return _progress_preview(self)

    def materialize(self):
        return _materialize(self)

    def final_catalog_targets(self) -> set[str]:
        return _final_catalog_targets(self)

    def final_catalog_metadata(self) -> dict[str, Any]:
        return _final_catalog_metadata(self)
