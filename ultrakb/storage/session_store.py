"""Authoritative immutable exchanges and independently accepted CTree prefixes."""

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from ctree.state import prefix_digest, validate_turns

from ultrakb.storage.blobs import put_blob, read_bytes, retain_blob
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import DatabaseIdentity
from ultrakb.storage.session_codec import encode_checkpoint, restore_checkpoint
from ultrakb.storage.tree_store import TreeStore, retain_tree


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    turn_count: int
    prefix_digest: str


class SessionConflict(ValueError):
    """A concurrent session edit invalidated the captured input."""


@dataclass(frozen=True)
class Conversation:
    session_id: str
    title: str
    view_id: str
    model: str
    language: str
    revision: int
    turns: tuple[dict, ...]


@dataclass(frozen=True)
class MemoryRoots:
    revision: int
    total_turns: int
    computed: Checkpoint | None
    projected: Checkpoint | None
    tree_id: str | None


def projection_fingerprint(session_id, policy_key, checkpoint):
    return hashlib.sha256(
        json.dumps(
            (
                session_id,
                policy_key,
                checkpoint.checkpoint_id,
                checkpoint.turn_count,
                checkpoint.prefix_digest,
            ),
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _projection_owner(session_id, policy_key):
    return hashlib.sha256(json.dumps((session_id, policy_key)).encode()).hexdigest()


def _bounded_metadata(*values):
    if len(json.dumps(values, ensure_ascii=False).encode()) > 63 * 1024:
        raise ValueError("Conversation metadata exceeds the encoded storage item limit")


class SessionStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    @staticmethod
    def validate_memory_policy(session_id: str, policy_key: str, model: str):
        if any(
            not isinstance(value, str) or not value for value in (session_id, policy_key, model)
        ):
            raise ValueError("Conversation memory requires an explicit session, model and policy")
        _bounded_metadata(session_id, policy_key, model)
        # Validate the largest later projection row before any billable SDK call.
        maximum_checkpoint = Checkpoint("f" * 32, 2**63 - 1, "f" * 64)
        _bounded_metadata(session_id, policy_key, maximum_checkpoint.__dict__)

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
        if not isinstance(title, str):
            raise ValueError("Conversation title must be text")
        _bounded_metadata(session_id, title, view_id, model, language)
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            connection.execute(
                "INSERT INTO ukb_conversations VALUES (?, ?, ?, ?, ?, 0, 0, 0)",
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
            or attempt_id is not None
            and (not isinstance(attempt_id, str) or not attempt_id)
        ):
            raise ValueError("A committed turn requires complete original messages")
        _bounded_metadata(session_id, turn_id, attempt_id or turn_id)
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
                "SELECT turn_count FROM ukb_conversations WHERE session_id = ?", (session_id,)
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
                    "UPDATE ukb_conversations SET revision = revision + 1, "
                    "turn_count = turn_count + 1 "
                    "WHERE session_id = ?",
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

    def load(self, session_id: str) -> Conversation:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT title, view_id, model, language, revision, turn_count "
                "FROM ukb_conversations "
                "WHERE session_id = ? AND deleted = 0",
                (session_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("Conversation was removed or never committed")
            turns = self._turns(db, session_id, row[5])
            if self._current(db, session_id) != row[4]:
                raise SessionConflict("Conversation changed during input capture")
            return Conversation(session_id, row[0], row[1], row[2], row[3], row[4], tuple(turns))

    def check_revision(self, session_id: str, expected: int):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            if self._current(db, session_id) != expected:
                raise SessionConflict("Conversation changed during context preparation")

    def has_recall_candidates(self, session_id: str, recent_turn_ids: set[str]) -> bool:
        if (
            not isinstance(recent_turn_ids, set)
            or len(recent_turn_ids) > 6
            or any(not isinstance(identity, str) for identity in recent_turn_ids)
        ):
            raise ValueError("Recall excludes at most the six existing recent turn identities")
        recent = sorted(recent_turn_ids)
        excluded = ",".join("?" for _ in recent)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._current(db, session_id)
            return (
                db.connection.execute(
                    "SELECT 1 FROM ukb_turns WHERE session_id = ? "
                    + (f"AND turn_id NOT IN ({excluded}) " if recent else "")
                    + "LIMIT 1",
                    (session_id, *recent),
                ).fetchone()
                is not None
            )

    def save_checkpoint(
        self,
        session_id: str,
        policy_key: str,
        snapshot: dict,
        *,
        expected_revision: int | None = None,
        check_cancelled=None,
    ) -> Checkpoint:
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
        _bounded_metadata(session_id, policy_key, count)
        if check_cancelled:
            check_cancelled()
        with storage_stage(self.root, self.identity) as db:
            if expected_revision is not None and self._current(db, session_id) != expected_revision:
                raise SessionConflict("Conversation changed before checkpoint acceptance")
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
            if check_cancelled:
                check_cancelled()
            with db.transaction() as connection:
                revision = self._current(db, session_id)
                if expected_revision is not None and revision != expected_revision:
                    raise SessionConflict("Conversation changed before checkpoint acceptance")
                if check_cancelled:
                    check_cancelled()
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

    def memory_roots(self, session_id: str, policy_key: str) -> MemoryRoots:
        owner = _projection_owner(session_id, policy_key)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT c.revision, c.turn_count, "
                "hc.checkpoint_id, hc.turn_count, hc.prefix_digest, "
                "pc.checkpoint_id, pc.turn_count, pc.prefix_digest, "
                "CASE WHEN ref.tree_id IS NOT NULL THEN t.tree_id END "
                "FROM ukb_conversations c LEFT JOIN ukb_session_heads h "
                "ON h.session_id = c.session_id AND h.policy_key = ? "
                "LEFT JOIN ukb_conversation_checkpoints hc ON hc.checkpoint_id = h.checkpoint_id "
                "LEFT JOIN ukb_session_projections p ON p.session_id = c.session_id "
                "AND p.policy_key = ? LEFT JOIN ukb_conversation_checkpoints pc "
                "ON pc.checkpoint_id = p.checkpoint_id LEFT JOIN ukb_tree_builds t "
                "ON t.tree_id = p.tree_id AND t.input_fingerprint = p.input_fingerprint "
                "AND t.protocol = 'chatindex' AND t.state = 'ready' "
                "LEFT JOIN ukb_tree_refs ref ON ref.tree_id = t.tree_id "
                "AND ref.owner_kind = 'session-projection' AND ref.owner_id = ? "
                "AND ref.slot = 'retrieval' WHERE c.session_id = ? AND c.deleted = 0",
                (policy_key, policy_key, owner, session_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("Conversation was removed or never committed")
            computed = Checkpoint(*row[2:5]) if row[2] else None
            projected = Checkpoint(*row[5:8]) if row[5] else None
            if (computed and computed.turn_count > row[1]) or (
                projected and (not computed or projected.turn_count > computed.turn_count)
            ):
                raise ValueError("Conversation memory roots have inconsistent prefix coverage")
            return MemoryRoots(row[0], row[1], computed, projected, row[8])

    def accept_projection(
        self, session_id, policy_key, checkpoint, exported, *, expected_revision, check_cancelled
    ):
        if not isinstance(exported, dict) or not isinstance(exported.get("basis"), dict):
            raise ValueError("Conversation projection requires a complete exported prefix")
        basis = exported["basis"]
        if (
            exported.get("conversation_id") != session_id
            or basis.get("turn_count") != checkpoint.turn_count
            or basis.get("turn_prefix_digest") != checkpoint.prefix_digest
            or basis.get("analysis_roles") != ["user"]
            or basis.get("content_roles") != ["user"]
        ):
            raise ValueError("Conversation projection must match its exact accepted checkpoint")
        fingerprint = projection_fingerprint(session_id, policy_key, checkpoint)
        owner = _projection_owner(session_id, policy_key)
        _bounded_metadata(session_id, policy_key, checkpoint.__dict__)
        check_cancelled()
        with storage_stage(self.root, self.identity) as db:
            tree = TreeStore(self.root, self.identity).build(
                exported,
                protocol="chatindex",
                input_fingerprint=fingerprint,
            )
            check_cancelled()
            with db.transaction() as connection:
                if self._current(db, session_id) != expected_revision:
                    raise SessionConflict("Conversation changed before projection acceptance")
                current = connection.execute(
                    "SELECT c.checkpoint_id, c.turn_count, c.prefix_digest "
                    "FROM ukb_session_heads h "
                    "JOIN ukb_conversation_checkpoints c ON c.checkpoint_id = h.checkpoint_id "
                    "WHERE h.session_id = ? AND h.policy_key = ?",
                    (session_id, policy_key),
                ).fetchone()
                if current != (
                    checkpoint.checkpoint_id,
                    checkpoint.turn_count,
                    checkpoint.prefix_digest,
                ):
                    raise SessionConflict(
                        "The accepted calculation prefix changed before projection"
                    )
                # This callback only checks the in-memory stop flag/deadline.
                check_cancelled()
                connection.execute(
                    "INSERT INTO ukb_session_projections VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id, policy_key) DO UPDATE SET "
                    "checkpoint_id = excluded.checkpoint_id, tree_id = excluded.tree_id, "
                    "input_fingerprint = excluded.input_fingerprint",
                    (session_id, policy_key, checkpoint.checkpoint_id, tree.tree_id, fingerprint),
                )
                connection.execute(
                    "DELETE FROM ukb_tree_refs WHERE owner_kind = 'session-projection' "
                    "AND owner_id = ? AND slot = 'retrieval'",
                    (owner,),
                )
                retain_tree(connection, "session-projection", owner, "retrieval", tree.tree_id)
        return tree.tree_id
