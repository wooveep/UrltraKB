"""On-demand SQLite indexes for durable compilation checkpoints and previews."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from typing import Any
from uuid import uuid4

from openkb.agent.checkpoint_artifacts import artifact_summary, valid_artifact_summary
from openkb.locks import file_write_lock
from openkb.sources import content_id, read_object, valid_id

_STAGES = frozenset({"facts", "planning", "generation", "verification"})


def index_path(store: Any, version_id: str) -> Any:
    """Return the per-version index path without creating a new database."""

    version = valid_id(version_id)
    directory = store.owned_path(store.root / "compilation" / "indexes")
    return store.owned_path(directory / f"{version}.sqlite3")


def index_exists(store: Any, version_id: str) -> bool:
    return index_path(store, version_id).is_file()


def index_lock_path(store: Any, version_id: str) -> Any:
    """Return the small per-version lease shared by writers and handoff repair."""

    path = index_path(store, version_id)
    return store.owned_path(path.with_name(path.name + ".lock"))


def pending_index_path(store: Any, version_id: str, key: str, storage: str) -> Any:
    """Name one recoverable handoff between a receipt file and its index row."""

    if storage not in {"checkpoint", "draft", "plan"}:
        raise ValueError("Invalid compilation artifact storage")
    version, key = valid_id(version_id), valid_id(key)
    directory = store.owned_path(store.root / "compilation" / "indexes" / f"{version}-pending")
    return store.owned_path(directory / f"{key}-{storage}.json")


def _schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS checkpoints (
            key TEXT PRIMARY KEY,
            stage TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS checkpoints_by_stage ON checkpoints(stage, key);

        CREATE TABLE IF NOT EXISTS artifacts (
            name TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            parse_id TEXT NOT NULL,
            storage TEXT NOT NULL,
            artifact_key TEXT NOT NULL,
            stage TEXT NOT NULL,
            adopted INTEGER NOT NULL CHECK(adopted IN (0, 1)),
            page_key TEXT,
            candidate TEXT,
            verified_page_key TEXT,
            verified_candidate TEXT,
            verified_checkpoint TEXT,
            verified_result TEXT,
            summary TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS artifacts_by_stage
        ON artifacts(source_id, version_id, parse_id, stage, storage, artifact_key);
        CREATE INDEX IF NOT EXISTS artifacts_by_page
        ON artifacts(source_id, version_id, parse_id, stage, page_key, adopted);
        CREATE INDEX IF NOT EXISTS artifacts_by_verification
        ON artifacts(source_id, version_id, parse_id, verified_page_key, verified_candidate);
        """
    )


def _summary_name(summary: dict[str, Any]) -> str:
    return summary["storage"] + ":" + summary["key"]


def _summary_values(summary: dict[str, Any], version_id: str) -> tuple[Any, ...]:
    """Validate one compact row before it can affect a durable listing."""

    if not valid_artifact_summary(summary):
        raise ValueError("Invalid compilation artifact index")
    source_id, version, parse_id = summary["source"], summary["version"], summary["parse"]
    valid_id(source_id, source=True)
    if valid_id(version) != valid_id(version_id) or not isinstance(parse_id, str):
        raise ValueError("Compilation artifact identity mismatch")
    valid_id(parse_id)
    storage, key, stage = summary["storage"], summary["key"], summary["stage"]
    valid_id(key)
    if stage not in _STAGES:
        raise ValueError("Invalid compilation artifact stage")
    page_key = summary.get("page_key") if isinstance(summary.get("page_key"), str) else None
    candidate = summary.get("candidate") if isinstance(summary.get("candidate"), str) else None
    verified = summary.get("verified")
    if not isinstance(verified, dict) or not all(
        isinstance(verified.get(field), str)
        for field in ("page_key", "candidate", "checkpoint", "result")
    ):
        verified = {}
    return (
        _summary_name(summary),
        source_id,
        version,
        parse_id,
        storage,
        key,
        stage,
        int(bool(summary.get("adopted"))),
        page_key,
        candidate,
        verified.get("page_key"),
        verified.get("candidate"),
        verified.get("checkpoint"),
        verified.get("result"),
        json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )


def _upsert_summary(db: sqlite3.Connection, summary: dict[str, Any], version_id: str) -> None:
    values = _summary_values(summary, version_id)
    db.execute(
        """
        INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            source_id = excluded.source_id,
            version_id = excluded.version_id,
            parse_id = excluded.parse_id,
            storage = excluded.storage,
            artifact_key = excluded.artifact_key,
            stage = excluded.stage,
            adopted = excluded.adopted,
            page_key = excluded.page_key,
            candidate = excluded.candidate,
            verified_page_key = excluded.verified_page_key,
            verified_candidate = excluded.verified_candidate,
            verified_checkpoint = excluded.verified_checkpoint,
            verified_result = excluded.verified_result,
            summary = excluded.summary
        """,
        values,
    )


def _migrate_legacy(db: sqlite3.Connection, legacy: dict[str, Any], version_id: str) -> None:
    """Import one older JSON index once; new writes never reread it."""

    keys = legacy.get("checkpoints", [])
    stages = legacy.get("stages", {})
    artifacts = legacy.get("artifacts", {})
    if (
        not isinstance(keys, list)
        or not isinstance(stages, dict)
        or not isinstance(artifacts, dict)
    ):
        raise ValueError("Invalid legacy compilation index")
    stage_for: dict[str, str] = {}
    for stage, selected in stages.items():
        if not isinstance(stage, str) or not isinstance(selected, list):
            raise ValueError("Invalid legacy compilation index")
        for key in selected:
            stage_for.setdefault(valid_id(key), stage if stage in _STAGES else "unknown")
    for key in keys:
        db.execute(
            "INSERT OR REPLACE INTO checkpoints(key, stage) VALUES (?, ?)",
            (valid_id(key), stage_for.get(key, "unknown")),
        )
    for summary in artifacts.values():
        if not isinstance(summary, dict):
            raise ValueError("Invalid legacy compilation index")
        _upsert_summary(db, summary, version_id)


def _receipt_input(record: dict[str, Any], version_id: str) -> dict[str, Any] | None:
    """Return an authenticated receipt identity for exceptional index rebuilds."""

    identity = record.get("input")
    if not isinstance(identity, dict):
        return None
    try:
        valid_id(identity.get("source"), source=True)
        if valid_id(identity.get("version")) != valid_id(version_id):
            return None
        valid_id(identity.get("parse"))
    except ValueError:
        return None
    return identity


def _rebuild_receipts(
    store: Any, version_id: str
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Read immutable receipt files once when a derived SQLite database is corrupt."""

    version = valid_id(version_id)
    root = store.owned_path(store.root / "compilation")
    checkpoints: list[tuple[str, str]] = []
    summaries: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            key = valid_id(path.stem)
            record = read_object(store.owned_path(path))
            if (
                _receipt_input(record, version) is None
                or record.get("key") != key
                or record.get("value_digest") != content_id(record.get("value"))
            ):
                continue
            contract = record.get("contract")
            if contract is not None and (
                not isinstance(contract, dict) or content_id(contract) != key
            ):
                continue
            payload = contract.get("payload", {}) if isinstance(contract, dict) else {}
            stage = payload.get("stage") if isinstance(payload, dict) else "unknown"
            checkpoints.append((key, stage if stage in _STAGES else "unknown"))
            if summary := artifact_summary(record, storage="checkpoint"):
                summaries.append(summary)
        except (FileNotFoundError, ValueError, TypeError):
            continue
    recovery = store.owned_path(root / "recovery")
    for storage in ("draft", "plan"):
        for path in sorted(recovery.glob(f"*-{storage}.json")):
            try:
                key = valid_id(path.name.removesuffix(f"-{storage}.json"))
                record = read_object(store.owned_path(path))
                if (
                    _receipt_input(record, version) is None
                    or record.get("key") != key
                    or record.get("kind") != storage
                    or record.get("digest") != content_id(record.get("value"))
                ):
                    continue
                if summary := artifact_summary(record, storage=storage):
                    summaries.append(summary)
            except (FileNotFoundError, ValueError, TypeError):
                continue
    return checkpoints, summaries


def _write_index(
    path: Any,
    version_id: str,
    *,
    checkpoint: tuple[str, str] | None = None,
    summary: dict[str, Any] | None = None,
    legacy: dict[str, Any] | None = None,
) -> None:
    """Write a valid database; the caller owns the version's index lease."""

    new_index = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        _schema(db)
        if new_index and legacy is not None:
            _migrate_legacy(db, legacy, version_id)
        if checkpoint is not None:
            key, stage = checkpoint
            if stage not in _STAGES:
                stage = "unknown"
            db.execute(
                "INSERT INTO checkpoints(key, stage) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET stage = excluded.stage",
                (valid_id(key), stage),
            )
        if summary is not None:
            _upsert_summary(db, summary, version_id)


def _rebuild_corrupt_index(store: Any, version_id: str) -> None:
    """Quarantine a corrupt projection, then rebuild it atomically from receipts."""

    version = valid_id(version_id)
    path = index_path(store, version)
    root = store.owned_path(store.root / "compilation")
    if path.exists():
        quarantine = store.owned_path(root / "quarantine")
        quarantine.mkdir(parents=True, exist_ok=True)
        path.replace(store.owned_path(quarantine / f"{version}-index-{uuid4().hex}.sqlite3"))
    checkpoints, summaries = _rebuild_receipts(store, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = store.owned_path(path.parent / f".{version}-{uuid4().hex}.tmp")
    try:
        with closing(sqlite3.connect(temporary)) as db, db:
            _schema(db)
            for checkpoint in checkpoints:
                key, stage = checkpoint
                db.execute("INSERT INTO checkpoints(key, stage) VALUES (?, ?)", (key, stage))
            for summary in summaries:
                _upsert_summary(db, summary, version)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def update_index(
    store: Any,
    version_id: str,
    *,
    checkpoint: tuple[str, str] | None = None,
    summary: dict[str, Any] | None = None,
    legacy: dict[str, Any] | None = None,
) -> None:
    """Atomically append one checkpoint and/or upsert one compact artifact row."""

    with file_write_lock(index_lock_path(store, version_id)):
        path = index_path(store, version_id)
        try:
            _write_index(path, version_id, checkpoint=checkpoint, summary=summary, legacy=legacy)
        except sqlite3.DatabaseError:
            _rebuild_corrupt_index(store, version_id)
            _write_index(path, version_id, checkpoint=checkpoint, summary=summary, legacy=legacy)


def _repair_corrupt_index(store: Any, version_id: str) -> None:
    """Serialize the exceptional replacement of an unreadable derived database."""

    with file_write_lock(index_lock_path(store, version_id)):
        _rebuild_corrupt_index(store, version_id)


def _pending_marker(
    store: Any, version_id: str, marker_path: Any
) -> tuple[dict[str, Any], str, str]:
    """Validate the durable half of a receipt-to-index handoff."""

    marker = read_object(marker_path)
    if not isinstance(marker.get("input"), dict) or marker.get("storage") not in {
        "checkpoint",
        "draft",
        "plan",
    }:
        raise ValueError("Invalid compilation index handoff")
    identity = marker["input"]
    source, version, parse = identity.get("source"), identity.get("version"), identity.get("parse")
    valid_id(source, source=True)
    if valid_id(version) != valid_id(version_id) or not isinstance(parse, str):
        raise ValueError("Compilation index handoff identity mismatch")
    valid_id(parse)
    key, storage = valid_id(marker.get("key")), marker["storage"]
    if marker_path != pending_index_path(store, version_id, key, storage):
        raise ValueError("Compilation index handoff identity mismatch")
    return identity, key, storage


def _pending_receipt(
    store: Any, identity: dict[str, Any], key: str, storage: str
) -> tuple[dict[str, Any], tuple[str, str] | None] | None:
    """Validate the receipt half of a pending handoff, if it committed."""

    root = store.owned_path(store.root / "compilation")
    suffix = "" if storage == "checkpoint" else "-" + storage
    receipt_path = store.owned_path(
        root / (f"{key}.json" if not suffix else f"recovery/{key}{suffix}.json")
    )
    if not receipt_path.exists():
        return None
    try:
        record = read_object(receipt_path)
    except FileNotFoundError:
        return None
    value = record.get("value")
    digest = "value_digest" if storage == "checkpoint" else "digest"
    if (
        record.get("input") != identity
        or record.get("key") != key
        or record.get(digest) != content_id(value)
        or (storage != "checkpoint" and record.get("kind") != storage)
    ):
        raise ValueError("Compilation index handoff receipt is invalid")
    if storage != "checkpoint":
        return record, None
    contract = record.get("contract")
    if contract is not None and (not isinstance(contract, dict) or content_id(contract) != key):
        raise ValueError("Compilation index handoff receipt is invalid")
    payload = contract.get("payload", {}) if isinstance(contract, dict) else {}
    stage = payload.get("stage") if isinstance(payload, dict) else "unknown"
    return record, (key, stage if isinstance(stage, str) else "unknown")


def pending_checkpoint_keys(
    store: Any, version_id: str, *, source_id: str | None = None
) -> list[str]:
    """Return receipt-authenticated checkpoint keys before a first index exists."""

    version = valid_id(version_id)
    if source_id is not None:
        valid_id(source_id, source=True)
    directory = store.owned_path(store.root / "compilation" / "indexes" / f"{version}-pending")
    if not directory.is_dir():
        return []
    keys = set()
    for marker_path in sorted(directory.glob("*.json")):
        try:
            marker_path = store.owned_path(marker_path)
            identity, key, storage = _pending_marker(store, version, marker_path)
            if storage != "checkpoint" or (
                source_id is not None and identity["source"] != source_id
            ):
                continue
            if _pending_receipt(store, identity, key, storage) is not None:
                keys.add(key)
        except (FileNotFoundError, ValueError, TypeError):
            continue
    return sorted(keys)


def recover_pending_entries(
    store: Any,
    version_id: str,
    *,
    source_id: str | None = None,
    parse_id: str | None = None,
) -> bool:
    """Finish receipt-to-index handoffs left by a process interruption.

    Normal writes remove their marker immediately. Recovery only scans this tiny
    pending directory, never the complete receipt or artifact history.
    """

    version = valid_id(version_id)
    # Artifact reads hold a shared KB lock, so recovery uses this independent
    # lease rather than attempting an illegal shared-to-exclusive KB upgrade.
    # Writers hold the same lease across marker -> receipt -> index, making a
    # marker an atomic handoff claim instead of a racing hint.
    with file_write_lock(index_lock_path(store, version)):
        if not index_exists(store, version):
            return False
        directory = store.owned_path(store.root / "compilation" / "indexes" / f"{version}-pending")
        if not directory.is_dir():
            return False
        recovered = False
        for marker_path in sorted(directory.glob("*.json")):
            marker_path = store.owned_path(marker_path)
            try:
                identity, key, storage = _pending_marker(store, version, marker_path)
            except (FileNotFoundError, ValueError, TypeError):
                # Markers are only a recoverable index handoff. A malformed or
                # stale one must never turn valid immutable receipts into an
                # unreadable source; retaining it adds no authenticated state.
                marker_path.unlink(missing_ok=True)
                continue
            if (source_id is not None and identity["source"] != source_id) or (
                parse_id is not None and identity["parse"] != parse_id
            ):
                # A source version may be parsed more than once. A stale handoff
                # for parse A cannot make an inspectable parse B unavailable.
                continue
            handoff = _pending_receipt(store, identity, key, storage)
            if handoff is None:
                # The receipt write itself did not commit, so this marker grants no
                # recovery action and must not block later successful attempts.
                marker_path.unlink(missing_ok=True)
                continue
            record, checkpoint = handoff
            update_index(
                store,
                version,
                checkpoint=checkpoint,
                summary=artifact_summary(record, storage=storage),
            )
            marker_path.unlink(missing_ok=True)
            recovered = True
        return recovered


def _read_checkpoint_keys(path: Any, stage: str | None) -> list[str]:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute(
                "SELECT 1 FROM checkpoints "
                "WHERE stage NOT IN ('facts', 'planning', 'generation', 'verification', 'unknown') "
                "LIMIT 1"
            ).fetchone():
                raise ValueError("Invalid compilation checkpoint index")
            if stage is None:
                rows = db.execute("SELECT key FROM checkpoints ORDER BY key").fetchall()
            else:
                rows = db.execute(
                    "SELECT key FROM checkpoints WHERE stage IN (?, 'unknown') ORDER BY key",
                    (stage,),
                ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise ValueError("Invalid compilation checkpoint index") from exc
    return [valid_id(key) for (key,) in rows]


def checkpoint_keys(store: Any, version_id: str, stage: str | None = None) -> list[str] | None:
    """Read only the keys needed for one resumed stage from a current index."""

    if stage is not None and stage not in _STAGES | {"unknown"}:
        raise ValueError("Invalid compilation checkpoint stage")
    path = index_path(store, version_id)
    if not path.is_file():
        return None
    recover_pending_entries(store, version_id)
    try:
        return _read_checkpoint_keys(path, stage)
    except ValueError:
        _repair_corrupt_index(store, version_id)
        return _read_checkpoint_keys(path, stage)


def _read_summary(value: str, source_id: str, version_id: str, parse_id: str) -> dict[str, Any]:
    try:
        summary = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid compilation artifact index") from exc
    if not valid_artifact_summary(summary) or (
        summary["source"],
        summary["version"],
        summary["parse"],
    ) != (source_id, version_id, parse_id):
        raise ValueError("Invalid compilation artifact index")
    return summary


def artifact_page(
    store: Any,
    source_id: str,
    version_id: str,
    parse_id: str,
    stage: str,
    *,
    offset: int,
    limit: int,
) -> tuple[tuple[dict[str, Any], ...], int] | None:
    """Fetch one UI page of compact summaries without hydrating historic rows."""

    if stage not in _STAGES:
        raise ValueError("Invalid compilation artifact stage")
    valid_id(source_id, source=True)
    valid_id(version_id)
    valid_id(parse_id)
    path = index_path(store, version_id)
    if not path.is_file():
        return None
    recover_pending_entries(store, version_id, source_id=source_id, parse_id=parse_id)
    include_verification = int(stage == "generation")
    where = """
        source_id = ? AND version_id = ? AND parse_id = ?
        AND (stage = ? OR (? = 1 AND stage = 'verification'))
        AND NOT (
            stage = 'generation' AND adopted = 0 AND page_key IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM artifacts adopted_page
                WHERE adopted_page.source_id = artifacts.source_id
                  AND adopted_page.version_id = artifacts.version_id
                  AND adopted_page.parse_id = artifacts.parse_id
                  AND adopted_page.stage = 'generation'
                  AND adopted_page.adopted = 1
                  AND adopted_page.page_key = artifacts.page_key
            )
        )
    """
    values = (source_id, version_id, parse_id, stage, include_verification)
    order = """
        CASE WHEN ? = 'generation' AND stage = 'verification' THEN 1 ELSE 0 END,
        CASE storage WHEN 'checkpoint' THEN 0 ELSE 1 END,
        artifact_key,
        name
    """
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            total = db.execute("SELECT COUNT(*) FROM artifacts WHERE " + where, values).fetchone()[
                0
            ]
            rows = db.execute(
                "SELECT storage, artifact_key, stage, name, summary FROM artifacts WHERE "
                + where
                + " ORDER BY "
                + order
                + " LIMIT ? OFFSET ?",
                (*values, stage, limit, offset),
            ).fetchall()
    except sqlite3.DatabaseError:
        _repair_corrupt_index(store, version_id)
        return artifact_page(
            store,
            source_id,
            version_id,
            parse_id,
            stage,
            offset=offset,
            limit=limit,
        )
    try:
        summaries = []
        for storage, artifact_key, indexed_stage, name, value in rows:
            summary = _read_summary(value, source_id, version_id, parse_id)
            if (storage, artifact_key, indexed_stage, name) != (
                summary["storage"],
                summary["key"],
                summary["stage"],
                _summary_name(summary),
            ):
                raise ValueError("Invalid compilation artifact index")
            summaries.append(summary)
        return tuple(summaries), total
    except ValueError:
        _repair_corrupt_index(store, version_id)
        return artifact_page(
            store,
            source_id,
            version_id,
            parse_id,
            stage,
            offset=offset,
            limit=limit,
        )


def artifact_summaries(
    store: Any,
    source_id: str,
    version_id: str,
    parse_id: str,
    *,
    stages: Iterable[str] | None = None,
) -> Iterator[dict[str, Any]] | None:
    """Stream selected compact summaries for non-UI projections."""

    path = index_path(store, version_id)
    if not path.is_file():
        return None
    recover_pending_entries(store, version_id, source_id=source_id, parse_id=parse_id)
    selected = tuple(stages or _STAGES)
    if not selected or any(stage not in _STAGES for stage in selected):
        raise ValueError("Invalid compilation artifact stage")

    def rows() -> Iterator[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in selected)
        query = (
            "SELECT storage, artifact_key, stage, name, summary FROM artifacts "
            "WHERE source_id = ? AND version_id = ? AND parse_id = ? "
            "AND stage IN (" + placeholders + ")"
        )
        cursor: tuple[int, str, str] | None = None
        repaired = False
        while True:
            values: list[Any] = [source_id, version_id, parse_id, *selected]
            resumed = query
            if cursor is not None:
                rank, artifact_key, name = cursor
                resumed += (
                    " AND (CASE storage WHEN 'checkpoint' THEN 0 ELSE 1 END > ? "
                    "OR (CASE storage WHEN 'checkpoint' THEN 0 ELSE 1 END = ? AND "
                    "(artifact_key > ? OR (artifact_key = ? AND name > ?))))"
                )
                values.extend((rank, rank, artifact_key, artifact_key, name))
            resumed += (
                " ORDER BY CASE storage WHEN 'checkpoint' THEN 0 ELSE 1 END, artifact_key, name"
            )
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                    records = db.execute(resumed, values)
                    for storage, artifact_key, stage, name, value in records:
                        summary = _read_summary(value, source_id, version_id, parse_id)
                        if (storage, artifact_key, stage, name) != (
                            summary["storage"],
                            summary["key"],
                            summary["stage"],
                            _summary_name(summary),
                        ):
                            raise ValueError("Invalid compilation artifact index")
                        cursor = (0 if storage == "checkpoint" else 1, artifact_key, name)
                        yield summary
                return
            except (sqlite3.DatabaseError, ValueError) as exc:
                if repaired:
                    raise ValueError("Invalid compilation artifact index") from exc
                _repair_corrupt_index(store, version_id)
                repaired = True

    return rows()


def verified_candidates(
    store: Any,
    source_id: str,
    version_id: str,
    parse_id: str,
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, str]] | None:
    """Load only verification receipts relevant to one visible artifact page."""

    path = index_path(store, version_id)
    if not path.is_file():
        return None
    candidates = {
        (row["page_key"], row["candidate"])
        for row in rows
        if isinstance(row.get("page_key"), str) and isinstance(row.get("candidate"), str)
    }
    if not candidates:
        return {}
    clauses = " OR ".join("(verified_page_key = ? AND verified_candidate = ?)" for _ in candidates)
    values = [source_id, version_id, parse_id]
    for page_key, candidate in sorted(candidates):
        values.extend((page_key, candidate))

    def query():
        result = {}
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            matches = db.execute(
                "SELECT summary FROM artifacts "
                "WHERE source_id = ? AND version_id = ? AND parse_id = ? "
                "AND stage = 'verification' AND (" + clauses + ")",
                values,
            )
            for (value,) in matches:
                summary = _read_summary(value, source_id, version_id, parse_id)
                verified = summary.get("verified")
                if isinstance(verified, dict):
                    result[verified["page_key"], verified["candidate"]] = {
                        "checkpoint": verified["checkpoint"],
                        "result": verified["result"],
                    }
        return result

    try:
        return query()
    except (sqlite3.DatabaseError, ValueError):
        _repair_corrupt_index(store, version_id)
        try:
            return query()
        except (sqlite3.DatabaseError, ValueError) as repaired:
            raise ValueError("Invalid compilation artifact index") from repaired


def legacy_artifact_summaries(
    store: Any, root: Any, checkpoints: Iterable[str]
) -> Iterator[dict[str, Any]]:
    """Read old full receipts serially only while migrating a pre-index source."""

    paths = [
        (store.owned_path(root / f"{valid_id(key)}.json"), valid_id(key), "checkpoint")
        for key in checkpoints
    ]
    recovery = store.owned_path(root / "recovery")
    paths.extend(
        (store.owned_path(path), valid_id(path.name.removesuffix("-draft.json")), "draft")
        for path in sorted(recovery.glob("*-draft.json"))
    )
    paths.extend(
        (store.owned_path(path), valid_id(path.name.removesuffix("-plan.json")), "plan")
        for path in sorted(recovery.glob("*-plan.json"))
    )
    for path, key, storage in paths:
        record = read_object(path)
        if not isinstance(record, dict) or record.get("key") != key:
            raise ValueError("Invalid legacy compilation artifact")
        value = record.get("value")
        digest = "value_digest" if storage == "checkpoint" else "digest"
        if record.get(digest) != content_id(value):
            raise ValueError("Compilation artifact digest mismatch")
        if storage == "checkpoint":
            contract = record.get("contract")
            if contract is not None and content_id(contract) != key:
                raise ValueError("Compilation artifact contract mismatch")
        elif record.get("kind") != storage:
            raise ValueError("Invalid legacy compilation artifact")
        summary = artifact_summary(record, storage=storage)
        if summary is not None:
            yield summary
