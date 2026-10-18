"""Authoritative immutable exchanges and independently accepted CTree prefixes."""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from ctree.state import prefix_digest, validate_turns

from ultrakb.storage.blobs import put_blob, read_bytes, retain_blob
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import DatabaseIdentity
from ultrakb.storage.session_codec import encode_checkpoint, restore_checkpoint


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    turn_count: int
    prefix_digest: str


class SessionStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def create(
        self,
        session_id: str,
        *,
        model: str,
        language: str,
        title: str = "",
        view_id: str = "legacy",
    ) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (session_id, model, language, view_id)
        ):
            raise ValueError("Conversation identity and execution settings must be explicit")
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            connection.execute(
                "INSERT INTO ukb_conversations VALUES (?, ?, ?, ?, ?, 0, 0)",
                (session_id, title, view_id, model, language),
            )

    @staticmethod
    def _current(db, session_id):
        row = db.connection.execute(
            "SELECT revision FROM ukb_conversations WHERE session_id = ? AND deleted = 0",
            (session_id,),
        ).fetchone()
        if not row:
            raise FileNotFoundError("Conversation was removed or never committed")
        return row[0]

    def append_exchange(
        self,
        session_id: str,
        turn_id: str,
        user: str,
        assistant: str,
        *,
        system: str | None = None,
        attempt_id: str | None = None,
    ) -> int:
        if (
            not isinstance(turn_id, str)
            or not turn_id
            or not isinstance(user, str)
            or not isinstance(assistant, str)
            or system is not None
            and not isinstance(system, str)
        ):
            raise ValueError("A committed turn requires complete original messages")
        messages = ([{"role": "system", "content": system}] if system is not None else []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]
        with storage_stage(self.root, self.identity) as db:
            revision = self._current(db, session_id)
            existing = db.connection.execute(
                "SELECT ordinal, payload_digest FROM ukb_turns "
                "WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if existing:
                retained = json.loads(read_bytes(db, existing[1]))
                if retained["messages"] != messages:
                    raise ValueError("Committed turn content is immutable")
                return existing[0]
            ordinal = db.connection.execute(
                "SELECT count(*) FROM ukb_turns WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            turn = {"turn_id": turn_id, "ordinal": ordinal, "messages": messages}
            content = put_blob(db, json.dumps(turn, ensure_ascii=False, sort_keys=True).encode())
            with db.transaction() as connection:
                if self._current(db, session_id) != revision:
                    raise ValueError("Conversation changed before this exchange was committed")
                connection.execute(
                    "INSERT INTO ukb_turns VALUES (?, ?, ?, ?, ?)",
                    (session_id, ordinal, turn_id, attempt_id or turn_id, content.digest),
                )
                retain_blob(
                    connection, "conversation-turn", f"{session_id}:{ordinal}", "original", content
                )
                connection.execute(
                    "UPDATE ukb_conversations SET revision = revision + 1 WHERE session_id = ?",
                    (session_id,),
                )
            return ordinal

    @staticmethod
    def _turns(db, session_id, count=None):
        SessionStore._current(db, session_id)
        rows = db.connection.execute(
            "SELECT turn_id, ordinal, payload_digest FROM ukb_turns WHERE session_id = ? "
            "AND (? IS NULL OR ordinal < ?) ORDER BY ordinal",
            (session_id, count, count),
        ).fetchall()
        turns = []
        for turn_id, ordinal, digest in rows:
            turn = json.loads(read_bytes(db, digest))
            if turn.get("turn_id") != turn_id or turn.get("ordinal") != ordinal:
                raise ValueError("Retained turn payload does not match its committed identity")
            turns.append(turn)
        validate_turns(turns)
        if count is not None and len(turns) != count:
            raise ValueError("Retained conversation prefix is incomplete")
        return turns

    def turns(self, session_id: str) -> list[dict]:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._turns(db, session_id)

    def save_checkpoint(self, session_id: str, policy_key: str, snapshot: dict) -> Checkpoint:
        count = snapshot.get("turn_count")
        if (
            type(count) is not int
            or count < 0
            or snapshot.get("conversation_id") != session_id
            or snapshot.get("analysis_roles") != ["user"]
            or not isinstance(policy_key, str)
            or not policy_key
        ):
            raise ValueError("Invalid checkpoint prefix size")
        with storage_stage(self.root, self.identity) as db:
            turns = self._turns(db, session_id, count)
            if turns != snapshot.get("turns") or snapshot.get(
                "turn_prefix_digest"
            ) != prefix_digest(turns):
                raise ValueError("Checkpoint must reference the exact authoritative turn prefix")
            prefix = prefix_digest(turns)
            existing = db.connection.execute(
                "SELECT checkpoint_id FROM ukb_conversation_checkpoints WHERE session_id = ? "
                "AND policy_key = ? AND prefix_digest = ?",
                (session_id, policy_key, prefix),
            ).fetchone()
            if existing:
                return Checkpoint(existing[0], count, prefix)
            # All SDK validation, encoding and Blob sync precede the short SQL write.
            encoded = encode_checkpoint(snapshot, session_id, policy_key)
            content = put_blob(db, encoded)
            checkpoint = Checkpoint(uuid.uuid4().hex, count, prefix)
            with db.transaction() as connection:
                self._current(db, session_id)
                connection.execute(
                    "INSERT INTO ukb_conversation_checkpoints VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        checkpoint.checkpoint_id,
                        session_id,
                        policy_key,
                        count,
                        prefix,
                        content.digest,
                    ),
                )
                retain_blob(
                    connection,
                    "conversation-checkpoint",
                    checkpoint.checkpoint_id,
                    "state",
                    content,
                )
                connection.execute(
                    "INSERT INTO ukb_session_heads (session_id, policy_key, checkpoint_id) "
                    "VALUES (?, ?, ?) ON CONFLICT(session_id, policy_key) DO UPDATE SET "
                    "checkpoint_id = excluded.checkpoint_id WHERE "
                    "(SELECT turn_count FROM ukb_conversation_checkpoints WHERE checkpoint_id = "
                    "ukb_session_heads.checkpoint_id) <= ?",
                    (session_id, policy_key, checkpoint.checkpoint_id, count),
                )
            return checkpoint

    def restore_checkpoint(self, session_id: str, policy_key: str, *, llm=None):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT c.turn_count, c.state_digest FROM ukb_session_heads h JOIN "
                "ukb_conversation_checkpoints c ON c.checkpoint_id = h.checkpoint_id "
                "WHERE h.session_id = ? AND h.policy_key = ?",
                (session_id, policy_key),
            ).fetchone()
            if row is None:
                raise KeyError("No accepted conversation checkpoint")
            return restore_checkpoint(
                read_bytes(db, row[1]),
                self._turns(db, session_id, row[0]),
                session_id,
                policy_key,
                llm=llm,
            )
