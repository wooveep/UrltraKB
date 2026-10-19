"""Bounded conversation summaries, fixed history pages and versioned tombstones."""

import hashlib
import json
from dataclasses import dataclass

from ultrakb.locks import LockCancelled, session_lock
from ultrakb.mutation import RecoveryRequired

from .blobs import read_bytes
from .coordination import storage_stage
from .execution_store import now
from .session_store import SessionConflict


def _limit(value):
    if type(value) is not int or not 1 <= value <= 128:
        raise ValueError("Conversation pages require 1 to 128 rows")


def _cursor(value, kinds):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid conversation cursor")
    fields = json.loads(value)
    if (
        not isinstance(fields, list)
        or len(fields) != len(kinds)
        or any(
            type(field) is not kind or kind is int and not 0 <= field < 2**63
            for field, kind in zip(fields, kinds, strict=True)
        )
    ):
        raise ValueError("Invalid conversation cursor")
    return fields


@dataclass(frozen=True)
class SessionHistory:
    session_id: str
    title: str
    model: str
    language: str
    version: str
    created_at: str
    total_turns: int
    start_ordinal: int
    older_before: int | None
    turns: tuple[dict, ...]
    metadata: tuple[dict, ...]
    pending: tuple[tuple[int, str], ...]
    next_pending: str | None


class SessionCatalog:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def _version(self, session_id, activity_version):
        return hashlib.sha256(
            json.dumps(
                (self.identity.kb_id, self.identity.generation, session_id, activity_version)
            ).encode()
        ).hexdigest()

    def list(self, *, view_id, limit=63, after=None, cancelled=None):
        _limit(limit)
        cursor = _cursor(after, (str, int))
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            rows = db.connection.execute(
                "SELECT session_id, title, turn_count, updated_at, model, view_id, "
                "sequence, activity_version FROM ukb_conversations "
                "WHERE view_id = ? AND deleted = 0 "
                + ("AND (updated_at, sequence) < (?, ?) " if cursor else "")
                + "ORDER BY updated_at DESC, sequence DESC LIMIT ?",
                (view_id, *(cursor or ()), limit),
            ).fetchall()
        return [
            {
                "id": row[0],
                "title": row[1],
                "turn_count": row[2],
                "updated_at": row[3],
                "model": row[4],
                "view_id": row[5],
                "cursor": json.dumps([row[3], row[6]]),
                "version": self._version(row[0], row[7]),
            }
            for row in rows
        ]

    def resolve(self, query, *, view_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            if query == "__latest__":
                rows = db.connection.execute(
                    "SELECT session_id FROM ukb_conversations WHERE view_id = ? AND deleted = 0 "
                    "ORDER BY updated_at DESC, sequence DESC LIMIT 1",
                    (view_id,),
                ).fetchall()
            else:
                exact = db.connection.execute(
                    "SELECT session_id FROM ukb_conversations WHERE view_id = ? AND deleted = 0 "
                    "AND session_id = ?",
                    (view_id, query),
                ).fetchone()
                if exact:
                    return exact[0]
                rows = db.connection.execute(
                    "SELECT session_id FROM ukb_conversations WHERE view_id = ? AND deleted = 0 "
                    "AND substr(session_id, 1, length(?)) = ? LIMIT 2",
                    (view_id, query, query),
                ).fetchall()
            if len(rows) > 1:
                raise ValueError("Conversation prefix is ambiguous")
            return rows[0][0] if rows else None

    def read(
        self, session_id, *, view_id, limit=63, before=None, pending_after=None, cancelled=None
    ):
        _limit(limit)
        if before is not None and (type(before) is not int or before < 0):
            raise ValueError("Conversation history requires a nonnegative ordinal cursor")
        pending_cursor = _cursor(pending_after, (int, int, int))
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            row = db.connection.execute(
                "SELECT title, model, language, activity_version, created_at, turn_count "
                "FROM ukb_conversations WHERE session_id = ? AND view_id = ? AND deleted = 0",
                (session_id, view_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("Conversation was removed or belongs to another view")
            end = min(before, row[5]) if before is not None else row[5]
            start = max(0, end - limit)
            rows = db.connection.execute(
                "SELECT t.ordinal, t.turn_id, t.payload_digest, r.result_digest FROM ukb_turns t "
                "LEFT JOIN ukb_turn_results r ON r.session_id = t.session_id "
                "AND r.ordinal = t.ordinal WHERE t.session_id = ? "
                "AND t.ordinal >= ? AND t.ordinal < ? ORDER BY t.ordinal LIMIT ?",
                (session_id, start, end, limit),
            ).fetchall()
            if len(rows) != end - start:
                raise RecoveryRequired("Committed conversation history is incomplete")
            turns, metadata = [], []
            for ordinal, turn_id, digest, result_digest in rows:
                turn = json.loads(read_bytes(db, digest))
                if turn.get("ordinal") != ordinal or turn.get("turn_id") != turn_id:
                    raise ValueError("Conversation payload does not match its committed identity")
                turns.append(turn)
                metadata.append(json.loads(read_bytes(db, result_digest)) if result_digest else {})
            pending_rows = db.connection.execute(
                "SELECT s.after_turn, s.message_digest, COALESCE(s.submission_order, s.sequence), "
                "s.sequence FROM ukb_conversation_submissions s WHERE s.session_id = ? "
                "AND s.after_turn >= ? AND (s.after_turn < ? OR (? = ? AND s.after_turn = ?)) "
                "AND NOT EXISTS (SELECT 1 FROM ukb_turns t WHERE t.session_id = s.session_id "
                "AND t.attempt_id = s.submission_id) "
                + (
                    "AND (s.after_turn, COALESCE(s.submission_order, s.sequence), s.sequence) "
                    "> (?, ?, ?) "
                    if pending_cursor
                    else ""
                )
                + "ORDER BY s.after_turn, COALESCE(s.submission_order, s.sequence), "
                "s.sequence LIMIT ?",
                (session_id, start, end, end, row[5], end, *(pending_cursor or ()), limit + 1),
            ).fetchall()
            pending = tuple(
                (position - start, read_bytes(db, digest).decode("utf-8"))
                for position, digest, _, _ in pending_rows[:limit]
            )
            current = db.connection.execute(
                "SELECT activity_version, deleted FROM ukb_conversations WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if current != (row[3], 0):
                raise SessionConflict("Conversation changed during history capture; reload it")
            last = pending_rows[limit - 1] if len(pending_rows) > limit else None
            return SessionHistory(
                session_id,
                row[0],
                row[1],
                row[2],
                self._version(session_id, row[3]),
                row[4],
                row[5],
                start,
                start if start else None,
                tuple(turns),
                tuple(metadata),
                pending,
                json.dumps([last[0], last[2], last[3]]) if last else None,
            )

    def delete(self, session_id, *, view_id, expected=None, cancelled=None, on_wait=None):
        with (
            session_lock(
                self.root,
                session_id,
                cancelled=cancelled,
                on_wait=on_wait,
                database_identity=self.identity,
            ),
            storage_stage(self.root, self.identity, cancelled=cancelled) as db,
        ):
            query = (
                "SELECT activity_version, deleted, view_id FROM ukb_conversations "
                "WHERE session_id = ?"
            )
            row = db.connection.execute(
                query,
                (session_id,),
            ).fetchone()
            if row is None or row[1]:
                return "missing"
            if row[2] != view_id:
                raise ValueError("Conversation belongs to another knowledge view")
            if expected is not None and expected != self._version(session_id, row[0]):
                return "conflict"
            timestamp = now()
            with db.transaction() as connection:
                if connection.execute(query, (session_id,)).fetchone() != row:
                    return "conflict"
                if cancelled and cancelled():
                    raise LockCancelled("Stopped before deleting the conversation")
                connection.execute(
                    "UPDATE ukb_conversations SET deleted = 1, updated_at = ?, "
                    "activity_version = activity_version + 1 WHERE session_id = ?",
                    (timestamp, session_id),
                )
            return "deleted"
