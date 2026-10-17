"""Rebuildable incremental counters over the authoritative request receipts.

The ledger lock serializes writers and readers. A durable intent precedes each
ledger replacement; replaying it applies the latest record exactly once. SQLite
is a disposable projection (stdlib only), never a second source of usage truth.
Live events omit request identities; terminal receipts still audit the ledger.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from pathlib import Path

from ultrakb.locks import _fsync_directory, atomic_record_lock, atomic_write_json

logger = logging.getLogger(__name__)
_TOKENS = ("input_total", "output_total", "cached_input", "reasoning_output")
_COUNTS = (
    *_TOKENS,
    *(f"missing_{key}" for key in _TOKENS),
    "requests",
    "actual_sends",
    "unsent_attempts",
    "send_unknown",
    "unknown_requests",
    "in_flight_requests",
    "collection_pending",
    "usage_pending",
)


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _counts(record):
    return {
        **{key: getattr(record, key) or 0 for key in _TOKENS},
        **{f"missing_{key}": int(getattr(record, key) is None) for key in _TOKENS},
        "requests": 1,
        "actual_sends": int(record.send_status == "sent"),
        "unsent_attempts": int(record.send_status == "not_sent"),
        "send_unknown": int(record.send_status == "unknown"),
        "unknown_requests": int(record.usage_status != "known"),
        "in_flight_requests": int(record.state == "started"),
        "collection_pending": int(record.observation != "transport"),
        "usage_pending": int(record.usage_status != "known" and record.send_status != "not_sent"),
    }


def _buckets(record):
    yield f"execution:{record.execution_id}"
    if record.source_id:
        yield f"source:{record.source_id}"
        if record.unit_id:
            yield f"unit:{record.source_id}:{record.unit_id}"
    else:
        yield f"unsourced:{record.execution_id}"


def _read_counter(connection, bucket):
    row = connection.execute(
        "SELECT value, digest FROM counters WHERE bucket=?", (bucket,)
    ).fetchone()
    if row is None:
        return dict.fromkeys(_COUNTS, 0)
    value, checksum = row
    if _digest(value) != checksum:
        raise sqlite3.DatabaseError("Usage projection checksum mismatch")
    decoded = json.loads(value)
    if set(decoded) != set(_COUNTS) or any(type(v) is not int or v < 0 for v in decoded.values()):
        raise sqlite3.DatabaseError("Invalid usage projection counters")
    return decoded


def _apply(connection, record):
    from ultrakb.llm_usage import UsageRequest

    encoded = _encode(record.model_dump(mode="json"))
    row = connection.execute(
        "SELECT value FROM requests WHERE identity=?", (record.request_attempt_id,)
    ).fetchone()
    if row and row[0] == encoded:
        return
    changes = {}
    previous = UsageRequest.model_validate_json(row[0]) if row else None
    for item, sign in ((previous, -1), (record, 1)):
        if item is None:
            continue
        delta = _counts(item)
        for bucket in _buckets(item):
            # Stage labels include work IDs. Keep the live projection bounded by
            # stage kind; the full audit retains each exact request/stage identity.
            for key in (bucket, f"{bucket}/stage:{item.stage.split(':', 1)[0]}"):
                counts = changes.setdefault(key, dict.fromkeys(_COUNTS, 0))
                for field, value in delta.items():
                    counts[field] += sign * value
    for bucket, delta in changes.items():
        counts = _read_counter(connection, bucket)
        value = _encode({key: counts[key] + delta[key] for key in _COUNTS})
        connection.execute(
            "INSERT OR REPLACE INTO counters VALUES (?, ?, ?)", (bucket, value, _digest(value))
        )
    connection.execute(
        "INSERT OR REPLACE INTO requests VALUES (?, ?)", (record.request_attempt_id, encoded)
    )


def _open(root):
    path = root / "projection.sqlite"
    if not path.exists():
        return _rebuild(root)
    connection = sqlite3.connect(path)
    try:
        if (root / "requests/.projection-invalid").exists():
            raise sqlite3.DatabaseError("Usage projection lost a write intent")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != 2:
            raise sqlite3.DatabaseError("Missing or unsupported usage projection")
        # Validate the schema even for an empty ledger.
        connection.execute("SELECT bucket FROM counters LIMIT 1").fetchone()
        connection.execute("SELECT identity FROM requests LIMIT 1").fetchone()
        stamp = connection.execute(
            "SELECT value FROM metadata WHERE key='ledger_mtime_ns'"
        ).fetchone()
        requests = root / "requests"
        current = requests.stat().st_mtime_ns if requests.exists() else 0
        # Canonical replacements change this directory identity. If the intent
        # could not be saved, the old projection cannot silently look current.
        if (stamp is None or int(stamp[0]) != current) and not any(
            (root / "projection-pending").glob("*.json")
        ):
            raise sqlite3.DatabaseError("Usage projection is behind its ledger")
        return connection
    except (sqlite3.DatabaseError, ValueError):
        connection.close()
        if path.exists():
            path.rename(root / f"projection-discarded-{uuid.uuid4().hex}.sqlite")
        return _rebuild(root)


def _rebuild(root):
    from ultrakb.llm_usage import UsageRequest

    connection = sqlite3.connect(root / "projection.sqlite")
    try:
        connection.executescript(
            "CREATE TABLE requests(identity TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE counters(bucket TEXT PRIMARY KEY, "
            "value TEXT NOT NULL, digest TEXT NOT NULL);"
            "CREATE TABLE metadata(key TEXT PRIMARY KEY, value INTEGER NOT NULL);"
            "PRAGMA user_version=2;"
        )
        with connection:
            for path in sorted((root / "requests").glob("*.json")):
                record = UsageRequest.model_validate_json(path.read_text("utf-8"))
                if path.stem != record.request_attempt_id:
                    raise ValueError("Usage record identity does not match its filename")
                _apply(connection, record)
            requests = root / "requests"
            (requests / ".projection-invalid").unlink(missing_ok=True)
            stamp = requests.stat().st_mtime_ns if requests.exists() else 0
            connection.execute("INSERT INTO metadata VALUES ('ledger_mtime_ns', ?)", (stamp,))
        return connection
    except BaseException:
        connection.close()
        (root / "projection.sqlite").unlink(missing_ok=True)
        raise


def _recover(root, connection):
    from ultrakb.llm_usage import UsageRequest

    for intent in (root / "projection-pending").glob("*.json"):
        path = root / "requests" / intent.name
        if path.exists():
            record = UsageRequest.model_validate_json(path.read_text("utf-8"))
            if record.request_attempt_id != intent.stem:
                raise ValueError("Invalid pending usage identity")
            with connection:
                _apply(connection, record)
        intent.unlink()
        _fsync_directory(intent.parent)
    requests = root / "requests"
    stamp = requests.stat().st_mtime_ns if requests.exists() else 0
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('ledger_mtime_ns', ?)", (stamp,)
        )


def write_request(kb_dir: Path, record) -> None:
    """Called with the ledger lock held. A projection failure never replays a send."""
    root = kb_dir / ".ultrakb/usage"
    identity = record.request_attempt_id
    intent = root / "projection-pending" / f"{identity}.json"
    try:
        atomic_write_json(intent, {"request_attempt_id": identity})
    except OSError:
        # Projection storage may fail independently. Always retain the provider
        # receipt; directory mtime admission forces reconstruction before use.
        # Keep the invalidation alongside canonical records so a cache-specific
        # directory failure cannot prevent a durable recovery signal.
        atomic_write_json(root / "requests/.projection-invalid", {"requires_rebuild": True})
        atomic_write_json(root / "requests" / f"{identity}.json", record.model_dump(mode="json"))
        logger.warning(
            "Usage saved without projection intent; projection will rebuild", exc_info=True
        )
        return
    atomic_write_json(root / "requests" / f"{identity}.json", record.model_dump(mode="json"))
    connection = None
    try:
        connection = _open(root)
        _recover(root, connection)
    except (OSError, ValueError, sqlite3.DatabaseError):
        logger.warning("Usage saved; incremental projection needs rebuilding", exc_info=True)
    finally:
        if connection is not None:
            connection.close()


def _aggregate(connection, buckets):
    totals = dict.fromkeys(_COUNTS, 0)
    stages = {}
    for bucket in sorted(set(buckets)):
        for key, value in _read_counter(connection, bucket).items():
            totals[key] += value
        rows = connection.execute(
            "SELECT bucket FROM counters WHERE bucket GLOB ?", (bucket + "/stage:*",)
        ).fetchall()
        for (key,) in rows:
            stage = key.split("/stage:", 1)[1]
            counts = _read_counter(connection, key)
            target = stages.setdefault(
                stage, dict.fromkeys(("requests", "input_total", "output_total"), 0)
            )
            for field in target:
                target[field] += counts[field]
    return {
        **{
            key: totals[key]
            for key in (
                *_TOKENS,
                "requests",
                "actual_sends",
                "unsent_attempts",
                "send_unknown",
                "unknown_requests",
                "in_flight_requests",
            )
        },
        "total_known": totals["input_total"] + totals["output_total"],
        "unknown_fields": {key: totals[f"missing_{key}"] for key in _TOKENS},
        "collection_complete": not totals["collection_pending"],
        "usage_complete": not totals["usage_pending"],
        "request_ids": [],
        "request_ids_complete": False,
        "stages": stages,
    }


def progress_receipt(kb_dir: Path, *, execution_ids, source_ids=(), unit_id=None) -> dict:
    """Bounded progress snapshot; exact request lists belong to terminal audit."""
    from ultrakb.llm_usage_execution import model_task_receipts
    from ultrakb.llm_usage_models import SourceUsageHistory

    root = kb_dir / ".ultrakb/usage"
    executions, sources = sorted(set(execution_ids)), sorted(set(source_ids))
    with atomic_record_lock(root / "ledger.lock"):
        connection = _open(root)
        try:
            try:
                _recover(root, connection)
                current = _aggregate(connection, [f"execution:{i}" for i in executions])
                cumulative = (
                    _aggregate(
                        connection,
                        [f"unit:{i}:{unit_id}" if unit_id else f"source:{i}" for i in sources]
                        + [f"unsourced:{i}" for i in executions],
                    )
                    if sources
                    else current
                )
            except sqlite3.DatabaseError:
                connection.close()
                (root / "projection.sqlite").rename(
                    root / f"projection-discarded-{uuid.uuid4().hex}.sqlite"
                )
                connection = _rebuild(root)
                _recover(root, connection)
                current = _aggregate(connection, [f"execution:{i}" for i in executions])
                cumulative = (
                    _aggregate(
                        connection,
                        [f"unit:{i}:{unit_id}" if unit_id else f"source:{i}" for i in sources]
                        + [f"unsourced:{i}" for i in executions],
                    )
                    if sources
                    else current
                )
        finally:
            connection.close()
    histories = []
    for source in sources:
        path = root / "sources" / f"{source}.json"
        histories.append(
            SourceUsageHistory.model_validate_json(path.read_text()).history_status
            if path.exists()
            else "unrecorded"
        )
    return {
        "execution_ids": executions,
        "source_ids": sources,
        "ledger": str(root),
        "model_tasks": model_task_receipts(kb_dir, executions),
        "current": current,
        "cumulative": cumulative,
        "history_status": "execution_only"
        if not sources
        else "recorded"
        if all(h == "recorded" for h in histories)
        else "partially_recorded"
        if len(set(histories)) > 1 or "partially_recorded" in histories
        else "unrecorded",
    }


def merge_progress(kb_dir: Path, receipts) -> dict:
    receipts = [receipt for receipt in receipts if receipt]
    return progress_receipt(
        kb_dir,
        execution_ids={i for receipt in receipts for i in receipt["execution_ids"]},
        source_ids={i for receipt in receipts for i in receipt.get("source_ids", [])},
    )
