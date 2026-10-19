"""Manual retention of replaced compiler diagnostics, never authoritative receipts."""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ultrakb.locks import LockCancelled
from ultrakb.source_records import Digest, RecordId

from .blobs import read_bytes
from .coordination import storage_stage
from .maintenance import _limits

_FROM = (
    "FROM ukb_compile_responses r JOIN ukb_compile_work w ON w.work_id = r.work_id "
    "JOIN ukb_executions e ON e.execution_id = r.execution_id "
    "JOIN ukb_blobs body ON body.digest = r.content_digest "
    "JOIN ukb_blobs metadata ON metadata.digest = r.metadata_digest "
)
_UNPUBLISHED = "SELECT 1 FROM ukb_knowledge_revisions WHERE state IN ('draft', 'sealed', 'ready')"
_SAFE = (
    f"NOT EXISTS ({_UNPUBLISHED}) AND "
    "EXISTS (SELECT 1 FROM ukb_compile_steps s WHERE s.work_id = r.work_id "
    "AND s.step_key = r.step_key AND s.request_digest = r.request_digest) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_compile_records c WHERE c.work_id = r.work_id "
    "AND c.step_key = r.step_key AND json_extract(c.record_json, '$.latest_response') "
    "= r.response_id) AND EXISTS (SELECT 1 FROM ukb_executions latest "
    "WHERE latest.sequence = (SELECT max(sequence) FROM ukb_executions "
    "WHERE task_id = e.task_id) AND latest.state = 'completed') "
    "AND NOT EXISTS (SELECT 1 FROM ukb_executions active WHERE active.task_id = e.task_id "
    "AND active.state IN ('started', 'stopping')) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_blob_refs extra WHERE extra.owner_kind = "
    "'compile-response' AND extra.owner_id = r.response_id AND NOT "
    "((extra.slot = 'body' AND extra.digest = r.content_digest) OR "
    "(extra.slot = 'metadata' AND extra.digest = r.metadata_digest)))"
)


@dataclass(frozen=True)
class DiagnosticRetentionPolicy:
    retention_days: int = 30
    payload_target_bytes: int = 512 * 1024**2

    def __post_init__(self):
        if type(self.retention_days) is not int or not 1 <= self.retention_days <= 36500:
            raise ValueError("Diagnostic retention requires 1 to 36500 days")
        if type(self.payload_target_bytes) is not int or not 1 <= self.payload_target_bytes < 2**63:
            raise ValueError("Diagnostic payload target requires a positive integer")


@dataclass(frozen=True)
class DiagnosticRetentionPreview:
    retention_days: int
    payload_target_bytes: int
    cutoff_at: str
    payload_bytes: int
    over_target_bytes: int
    candidate_count: int
    candidate_reference_bytes: int
    protected_count: int
    protected_reference_bytes: int
    selected_count: int
    selected_reference_bytes: int
    has_more: bool
    blocked_reason: str | None = None


@dataclass(frozen=True)
class DiagnosticArchiveResult:
    status: str
    archived_count: int = 0
    released_reference_bytes: int = 0
    has_more: bool = False
    blocked_reason: str | None = None


class _AcceptedResponseReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    record_file: Annotated[str, Field(pattern=r"^compile-response/[0-9a-f]{32}$")]
    response_file: Annotated[str, Field(pattern=r"^blob/[0-9a-f]{64}$")]
    response_sha256: Digest
    execution_id: RecordId
    awaiting_validation: bool


class _AcceptedMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    acceptance_version: str
    metadata: dict[str, object]
    reused_response: _AcceptedResponseReference | None


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise LockCancelled("Stopped while preparing diagnostic retention")


def _accepted_links(db, cancelled):
    from ultrakb.agent.compile_contract import ACCEPTANCE_VERSION

    identifiers, digests = set(), set()
    cursor = db.connection.execute("SELECT metadata_json FROM ukb_compile_steps")
    try:
        for (encoded,) in cursor:
            _check_cancelled(cancelled)
            descriptor = json.loads(encoded)
            if not isinstance(descriptor, dict) or set(descriptor) != {"metadata_digest"}:
                raise ValueError("Unknown accepted metadata descriptor")
            digest = TypeAdapter(Digest).validate_python(descriptor["metadata_digest"])
            size = db.connection.execute(
                "SELECT byte_count FROM ukb_blobs WHERE digest = ?", (digest,)
            ).fetchone()
            if size is None or size[0] > 64 * 1024:
                raise ValueError("Accepted metadata cannot be bounded for retention")
            metadata = _AcceptedMetadata.model_validate_json(read_bytes(db, digest))
            if metadata.acceptance_version != ACCEPTANCE_VERSION:
                raise ValueError("Unsupported accepted metadata")
            reused = metadata.reused_response
            if reused is None:
                continue
            if reused.response_file != "blob/" + reused.response_sha256:
                raise ValueError("Accepted payload references disagree")
            identifiers.add(reused.record_file.removeprefix("compile-response/"))
            digests.add(reused.response_sha256)
    except LockCancelled:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        return identifiers, digests, "accepted_metadata_unavailable"
    finally:
        cursor.close()
    return identifiers, digests, None


def _rows(db, cancelled):
    cursor = db.connection.execute(
        "SELECT r.response_id, r.work_id, r.step_key, r.content_digest, r.metadata_digest, "
        f"body.byte_count, metadata.byte_count, r.created_at, CASE WHEN {_SAFE} THEN 1 ELSE 0 END, "
        "(SELECT max(sequence) FROM ukb_executions WHERE task_id = e.task_id) "
        + _FROM
        + "ORDER BY julianday(r.created_at), r.response_id"
    )
    try:
        for row in cursor:
            _check_cancelled(cancelled)
            yield row
    finally:
        cursor.close()


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _plan(db, policy, max_items, max_bytes, cancelled):
    cutoff = datetime.now(timezone.utc) - timedelta(days=policy.retention_days)
    if db.connection.execute(_UNPUBLISHED + " LIMIT 1").fetchone():
        protected_ids, protected_digests, reason = set(), set(), "unpublished_revision"
    else:
        protected_ids, protected_digests, reason = _accepted_links(db, cancelled)
    counts, sizes, total, total_references = Counter(), {}, 0, 0
    for row in _rows(db, cancelled):
        total += 1
        total_references += row[5] + row[6]
        for digest, size in zip(row[3:5], row[5:7], strict=True):
            counts[digest] += 1
            sizes[digest] = size
    payload_bytes = remaining_payload = sum(sizes.values())
    candidate_count = candidate_bytes = selected_bytes = encoded_bytes = 0
    selected = []
    for row in _rows(db, cancelled):
        timestamp = _timestamp(row[7])
        if (
            reason
            or not row[8]
            or timestamp is None
            or row[0] in protected_ids
            or any(digest in protected_digests for digest in row[3:5])
        ):
            continue
        if timestamp >= cutoff and remaining_payload <= policy.payload_target_bytes:
            continue
        size = row[5] + row[6]
        candidate_count += 1
        candidate_bytes += size
        for digest in row[3:5]:
            counts[digest] -= 1
            if counts[digest] == 0:
                remaining_payload -= sizes[digest]
        cost = len(json.dumps((*row[:5], row[9])).encode()) + 512
        if (
            len(selected) < min(max_items, 32)
            and selected_bytes + size <= max_bytes
            and encoded_bytes + cost <= 256 * 1024
        ):
            selected.append(row)
            selected_bytes += size
            encoded_bytes += cost
    return DiagnosticRetentionPreview(
        policy.retention_days,
        policy.payload_target_bytes,
        cutoff.isoformat(),
        payload_bytes,
        max(0, payload_bytes - policy.payload_target_bytes),
        candidate_count,
        candidate_bytes,
        total - candidate_count,
        total_references - candidate_bytes,
        len(selected),
        selected_bytes,
        len(selected) < candidate_count,
        reason,
    ), selected


class DiagnosticRetention:
    def __init__(self, root, identity, *, policy=None):
        self.root, self.identity = root.resolve(), identity
        self.policy = policy or DiagnosticRetentionPolicy()
        if type(self.policy) is not DiagnosticRetentionPolicy:
            raise ValueError("Diagnostic retention requires its validated policy")

    def preview(self, *, max_items=128, max_bytes=64 * 1024**2, cancelled=None):
        _limits(max_items, max_bytes)
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            db.connection.execute("BEGIN")
            return _plan(db, self.policy, max_items, max_bytes, cancelled)[0]

    def archive(self, *, max_items=128, max_bytes=64 * 1024**2, cancelled=None):
        _limits(max_items, max_bytes)
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            db.connection.execute("BEGIN")
            preview, rows = _plan(db, self.policy, max_items, max_bytes, cancelled)
        if preview.blocked_reason:
            return DiagnosticArchiveResult("blocked", blocked_reason=preview.blocked_reason)
        if not rows:
            return DiagnosticArchiveResult("completed", has_more=preview.has_more)
        count = byte_count = 0
        with storage_stage(self.root, self.identity, cancelled=cancelled) as db:
            with db.transaction() as connection:
                for row in rows:
                    changed = connection.execute(
                        "DELETE FROM ukb_compile_responses AS r WHERE r.response_id = ? "
                        "AND EXISTS (SELECT 1 FROM ukb_executions e WHERE "
                        "e.execution_id = r.execution_id AND (SELECT max(sequence) FROM "
                        f"ukb_executions WHERE task_id = e.task_id) = ? AND {_SAFE})",
                        (row[0], row[9]),
                    ).rowcount
                    if not changed:
                        continue
                    connection.execute(
                        "DELETE FROM ukb_blob_refs WHERE owner_kind = 'compile-response' "
                        "AND owner_id = ? AND ((slot = 'body' AND digest = ?) "
                        "OR (slot = 'metadata' AND digest = ?))",
                        (row[0], row[3], row[4]),
                    )
                    connection.execute(
                        "UPDATE ukb_compile_work SET diagnostics_retired_count = "
                        "diagnostics_retired_count + 1, diagnostics_retired_bytes = "
                        "diagnostics_retired_bytes + ? WHERE work_id = ?",
                        (row[5] + row[6], row[1]),
                    )
                    count += 1
                    byte_count += row[5] + row[6]
        return DiagnosticArchiveResult(
            "completed", count, byte_count, has_more=preview.candidate_count > count
        )
