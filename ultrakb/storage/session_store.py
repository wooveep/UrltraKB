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
from ultrakb.storage.execution_store import execution_state, now, require_active_execution
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
    created_at: str = ""
    updated_at: str = ""
    activity_version: int = 0


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
        draft: bool = False,
    ) -> None:
        if (
            any(not isinstance(value, str) for value in (session_id, model, language, view_id))
            or not session_id
            or not view_id
            or (not draft and (not model or not language))
        ):
            raise ValueError("Conversation identity and execution settings must be explicit")
        if not isinstance(title, str):
            raise ValueError("Conversation title must be text")
        _bounded_metadata(session_id, title, view_id, model, language)
        timestamp = now()
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            connection.execute(
                "INSERT INTO ukb_conversations(session_id, title, view_id, model, language, "
                "revision, turn_count, deleted, created_at, updated_at, activity_version) "
                "VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?, 0)",
                (session_id, title, view_id, model, language, timestamp, timestamp),
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

    def _execution_state(self, connection, execution_id):
        try:
            return execution_state(connection, self.identity, execution_id)
        except ValueError as error:
            raise SessionConflict(str(error)) from error

    def _require_active_execution(self, connection, execution_id):
        try:
            require_active_execution(connection, self.identity, execution_id)
        except ValueError as error:
            raise SessionConflict(str(error)) from error

    def queue_submission(
        self,
        session_id,
        submission_id,
        message,
        *,
        view_id,
        model="",
        language="",
        new=False,
        order=None,
        after_turn=0,
        cancelled=None,
        on_wait=None,
    ):
        """Retain stopped desktop intent without creating an execution or replaying work."""
        if (
            not isinstance(message, str)
            or not message.strip()
            or not isinstance(submission_id, str)
            or not submission_id
            or type(after_turn) is not int
            or after_turn < 0
            or order is not None
            and (type(order) is not int or order < 0)
        ):
            raise ValueError("Invalid queued conversation submission")
        _bounded_metadata(session_id, submission_id, view_id, model, language, order)
        from ultrakb.locks import session_lock

        with (
            session_lock(
                self.root,
                session_id,
                database_identity=self.identity,
                cancelled=cancelled,
                on_wait=on_wait,
            ),
            storage_stage(self.root, self.identity, cancelled=cancelled) as db,
        ):
            row = db.connection.execute(
                "SELECT deleted, view_id FROM ukb_conversations WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                if not new:
                    return "missing"
                self.create(
                    session_id,
                    view_id=view_id,
                    model=model,
                    language=language,
                    title=" ".join(message.split())[:60],
                    draft=True,
                )
            elif row[0]:
                return "deleted"
            elif row[1] != view_id:
                raise ValueError("Conversation belongs to another knowledge view")
            content = put_blob(db, message.encode("utf-8"))
            owner = hashlib.sha256(json.dumps((session_id, submission_id)).encode()).hexdigest()
            with db.transaction() as connection:
                self._retain_submission(
                    connection,
                    session_id,
                    submission_id,
                    content,
                    order,
                    owner=owner,
                    after_turn=after_turn,
                )
            return "retained"

    def _retain_submission(
        self, connection, session_id, submission_id, content, order, *, owner, after_turn=None
    ):
        existing = connection.execute(
            "SELECT message_digest, submission_order FROM ukb_conversation_submissions "
            "WHERE session_id = ? AND submission_id = ?",
            (session_id, submission_id),
        ).fetchone()
        if existing:
            if existing != (content.digest, order):
                raise ValueError("Conversation submission identity mismatch")
            return
        count = connection.execute(
            "SELECT turn_count FROM ukb_conversations WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO ukb_conversation_submissions(session_id, submission_id, "
            "message_digest, after_turn, submission_order) VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                submission_id,
                content.digest,
                count if after_turn is None else min(after_turn, count),
                order,
            ),
        )
        retain_blob(connection, "conversation-submission", owner, "user", content)
        connection.execute(
            "UPDATE ukb_conversations SET updated_at = ?, activity_version = activity_version + 1 "
            "WHERE session_id = ?",
            (now(), session_id),
        )

    def start_submission(
        self,
        session_id,
        submission_id,
        message,
        *,
        execution_id,
        publication_id,
        order=None,
        model=None,
        language=None,
    ):
        if (
            not isinstance(submission_id, str)
            or not submission_id
            or not isinstance(message, str)
            or not message.strip()
            or order is not None
            and (type(order) is not int or order < 0)
        ):
            raise ValueError("Conversation submissions require original text and a stable identity")
        _bounded_metadata(session_id, submission_id, execution_id, publication_id, order)
        timestamp = now()
        with storage_stage(self.root, self.identity) as db:
            content = put_blob(db, message.encode("utf-8"))
            owner = hashlib.sha256(json.dumps((session_id, submission_id)).encode()).hexdigest()
            with db.transaction() as connection:
                self._current(db, session_id)
                self._require_active_execution(connection, execution_id)
                if model is not None or language is not None:
                    if (
                        not isinstance(model, str)
                        or not model
                        or not isinstance(language, str)
                        or not language
                    ):
                        raise ValueError("Executed conversations require explicit model settings")
                    connection.execute(
                        "UPDATE ukb_conversations SET model = ?, language = ? "
                        "WHERE session_id = ? AND turn_count = 0 AND model = '' AND language = ''",
                        (model, language, session_id),
                    )
                self._retain_submission(
                    connection, session_id, submission_id, content, order, owner=owner
                )
                completed = connection.execute(
                    "SELECT ordinal FROM ukb_turns WHERE session_id = ? AND attempt_id = ?",
                    (session_id, submission_id),
                ).fetchone()
                connection.execute(
                    "INSERT INTO ukb_conversation_runs VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (
                        execution_id,
                        session_id,
                        submission_id,
                        publication_id,
                        "reused" if completed else "started",
                        completed[0] if completed else None,
                    ),
                )
                connection.execute(
                    "UPDATE ukb_conversations SET updated_at = ?, "
                    "activity_version = activity_version + 1 "
                    "WHERE session_id = ?",
                    (timestamp, session_id),
                )

    def completed_submission(self, session_id, submission_id, message, *, execution_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._execution_state(db.connection, execution_id)
            row = db.connection.execute(
                "SELECT t.ordinal, t.payload_digest, r.result_digest FROM ukb_turns t "
                "LEFT JOIN ukb_turn_results r ON r.session_id = t.session_id "
                "AND r.ordinal = t.ordinal "
                "JOIN ukb_conversation_runs run ON run.session_id = t.session_id "
                "AND run.submission_id = t.attempt_id AND run.ordinal = t.ordinal "
                "WHERE t.session_id = ? AND t.attempt_id = ? AND run.execution_id = ? "
                "AND run.state IN ('completed', 'reused')",
                (session_id, submission_id, execution_id),
            ).fetchone()
            if row is None:
                return None
            original = json.loads(read_bytes(db, row[1]))
            messages = {m["role"]: m["content"] for m in original["messages"]}
            if messages["user"] != message:
                raise ValueError("Conversation submission identity mismatch")
            metadata = json.loads(read_bytes(db, row[2])) if row[2] else {}
            return {"answer": messages["assistant"], "ordinal": row[0], "metadata": metadata}

    def finish_submission(self, execution_id, state, *, error_code=None):
        if state not in {"failed", "stopped", "blocked"}:
            raise ValueError("Conversation execution requires an actual terminal result")
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            self._execution_state(connection, execution_id)
            connection.execute(
                "UPDATE ukb_conversation_runs SET state = ?, error_code = ? "
                "WHERE execution_id = ? AND state = 'started'",
                (state, error_code, execution_id),
            )

    def append_exchange(
        self,
        session_id: str,
        turn_id: str,
        user: str,
        assistant: str,
        *,
        system: str | None = None,
        attempt_id: str | None = None,
        expected_revision: int | None = None,
        check_cancelled=None,
        metadata: dict | None = None,
        execution_id: str | None = None,
        reused_from: str | None = None,
    ) -> int:
        if (
            not isinstance(turn_id, str)
            or not turn_id
            or not isinstance(user, str)
            or not isinstance(assistant, str)
            or not user.strip()
            or not assistant.strip()
            or system is not None
            and not isinstance(system, str)
            or attempt_id is not None
            and (not isinstance(attempt_id, str) or not attempt_id)
        ):
            raise ValueError("A committed turn requires complete original messages")
        _bounded_metadata(session_id, turn_id, attempt_id or turn_id)
        timestamp = now()
        messages = ([{"role": "system", "content": system}] if system is not None else []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]
        with storage_stage(self.root, self.identity) as db:
            if execution_id is not None:
                self._execution_state(db.connection, execution_id)
            revision = self._current(db, session_id)
            if expected_revision is not None and revision != expected_revision:
                raise SessionConflict("Conversation changed before turn acceptance")
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
            result = (
                put_blob(db, json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode())
                if metadata is not None
                else None
            )
            if execution_id is not None:
                if result is None:
                    raise ValueError("A complete application turn requires its answer metadata")
                # Independent acceptance is the paid computation's recovery point.
                # The later atomic turn commit can fail without losing this answer.
                with db.transaction() as connection:
                    if self._current(db, session_id) != revision:
                        raise SessionConflict("Conversation changed before answer acceptance")
                    self._require_active_execution(connection, execution_id)
                    run = connection.execute(
                        "SELECT session_id, submission_id, state FROM ukb_conversation_runs "
                        "WHERE execution_id = ?",
                        (execution_id,),
                    ).fetchone()
                    if run != (session_id, attempt_id, "started"):
                        raise SessionConflict(
                            "Answer acceptance requires its exact active submission"
                        )
                    if reused_from is not None:
                        original = connection.execute(
                            "SELECT a.turn_id, a.input_revision, a.ordinal, a.payload_digest, "
                            "a.result_digest FROM ukb_conversation_answers a "
                            "JOIN ukb_conversation_runs r "
                            "ON r.execution_id = a.execution_id WHERE a.execution_id = ? "
                            "AND r.session_id = ? AND r.submission_id = ?",
                            (reused_from, session_id, attempt_id),
                        ).fetchone()
                        if original != (turn_id, revision, ordinal, content.digest, result.digest):
                            raise SessionConflict(
                                "Answer reuse must match the exact retained computation"
                            )
                    if check_cancelled:
                        check_cancelled()
                    connection.execute(
                        "INSERT INTO ukb_conversation_answers VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            execution_id,
                            turn_id,
                            revision,
                            ordinal,
                            content.digest,
                            result.digest,
                            reused_from,
                        ),
                    )
                    retain_blob(
                        connection, "conversation-answer", execution_id, "original", content
                    )
                    retain_blob(connection, "conversation-answer", execution_id, "result", result)
            with db.transaction() as connection:
                if self._current(db, session_id) != revision:
                    raise ValueError("Conversation changed before this exchange was committed")
                if check_cancelled:
                    check_cancelled()
                if execution_id is not None:
                    self._require_active_execution(connection, execution_id)
                connection.execute(
                    "INSERT INTO ukb_turns VALUES (?, ?, ?, ?, ?)",
                    (session_id, ordinal, turn_id, attempt_id or turn_id, content.digest),
                )
                retain_blob(
                    connection, "conversation-turn", f"{session_id}:{ordinal}", "original", content
                )
                if result is not None:
                    connection.execute(
                        "INSERT INTO ukb_turn_results VALUES (?, ?, ?)",
                        (session_id, ordinal, result.digest),
                    )
                    retain_blob(
                        connection, "conversation-turn", f"{session_id}:{ordinal}", "result", result
                    )
                if (
                    execution_id is not None
                    and connection.execute(
                        "UPDATE ukb_conversation_runs SET state = ?, ordinal = ? "
                        "WHERE execution_id = ? AND session_id = ? AND submission_id = ? "
                        "AND state = 'started'",
                        (
                            "reused" if reused_from else "completed",
                            ordinal,
                            execution_id,
                            session_id,
                            attempt_id,
                        ),
                    ).rowcount
                    != 1
                ):
                    raise SessionConflict("The accepted turn must finish its exact active attempt")
                connection.execute(
                    "UPDATE ukb_conversations SET revision = revision + 1, "
                    "turn_count = turn_count + 1, updated_at = ?, "
                    "activity_version = activity_version + 1 "
                    "WHERE session_id = ?",
                    (timestamp, session_id),
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
                "SELECT title, view_id, model, language, revision, turn_count, "
                "created_at, updated_at, activity_version "
                "FROM ukb_conversations "
                "WHERE session_id = ? AND deleted = 0",
                (session_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("Conversation was removed or never committed")
            turns = self._turns(db, session_id, row[5])
            if self._current(db, session_id) != row[4]:
                raise SessionConflict("Conversation changed during input capture")
            return Conversation(
                session_id, row[0], row[1], row[2], row[3], row[4], tuple(turns), *row[6:9]
            )

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
