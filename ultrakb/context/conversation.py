"""Incrementally commit ChatIndex checkpoints and ConDB user-only retrieval trees."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from contextdb import ConDB
from ctree import CTree
from ctree.prompts import PROMPT_VERSION

from ultrakb.agent.chat_session import ChatSession, deletion_marker
from ultrakb.context.projection import fingerprint, open_runtime, runtime_database
from ultrakb.conversation_index import ChatIndexLLM, create_conversation_index
from ultrakb.lifecycle import current_generation
from ultrakb.llm_execution import CompletionExecutor, raise_if_terminal

CHECKPOINT_VERSION = 1
CONTENT_ROLES = ("user",)


@dataclass(frozen=True)
class MemoryState:
    policy_key: str
    tree_id: str | None
    indexed_turns: int
    pending_turns: int
    prefix_digest: str
    diagnostics: tuple[str, ...] = ()


def _schema(db):
    with db.transaction() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS okb_conversation_checkpoints (
            session_id TEXT NOT NULL, policy_key TEXT NOT NULL,
            prefix_json TEXT NOT NULL, checkpoint_json TEXT NOT NULL,
            tree_id TEXT NOT NULL REFERENCES trees(tree_id) ON DELETE CASCADE,
            PRIMARY KEY (session_id, policy_key))""")


class ConversationMemory:
    """The caller owns session → KB leases; workers receive an immutable committed prefix."""

    def __init__(self, session: ChatSession, executor: CompletionExecutor):
        self.session, self.executor = session, executor
        self.kb_dir = session.path.parent.parent.parent
        self.generation = current_generation(self.kb_dir)
        self.policy_key = fingerprint(
            {
                "kb_generation": self.generation,
                "session": session.id,
                "view": session.view_id,
                "models": executor.bindings.public_identity(),
                "endpoint": fingerprint(executor.bindings.credentials().base_url),
                "analysis_roles": CONTENT_ROLES,
                "content_roles": CONTENT_ROLES,
                "checkpoint_version": CHECKPOINT_VERSION,
                "prompt_version": PROMPT_VERSION,
            }
        )
        self.prefix = [
            {
                "turn_id": t.turn_id,
                "order": t.order,
                "content_digest": t.content_digest,
            }
            for t in session.turns
        ]

    def check_current(self) -> None:
        self.executor.policy.check()
        if current_generation(self.kb_dir) != self.generation:
            raise FileNotFoundError("Conversation knowledge base was replaced")
        if deletion_marker(self.kb_dir, self.session.id).exists():
            raise FileNotFoundError("Conversation was deleted")
        if self.session._version is None or (
            hashlib.sha256(self.session.path.read_bytes()).hexdigest() != self.session._version
        ):
            raise ValueError("Conversation changed during context preparation")

    def _restore(self, db):
        row = db.conn.execute(
            "SELECT prefix_json, checkpoint_json, tree_id FROM okb_conversation_checkpoints "
            "WHERE session_id=? AND policy_key=?",
            (self.session.id, self.policy_key),
        ).fetchone()
        if row:
            try:
                prefix, snapshot = json.loads(row[0]), json.loads(row[1])
                if prefix != self.prefix[: len(prefix)] or len(snapshot["turns"]) != len(prefix):
                    raise ValueError("Checkpoint no longer matches committed turns")
                tree = CTree.restore_state(snapshot, llm=ChatIndexLLM(self.executor))
                if tree.conversation_id != self.session.id or tree.analysis_roles != CONTENT_ROLES:
                    raise ValueError("Checkpoint belongs to another conversation or role policy")
                for original, indexed in zip(self.session.turns, tree.turns):
                    if indexed["turn_id"] != original.turn_id or indexed["messages"] != [
                        {"role": "user", "content": original.user_text},
                        {"role": "assistant", "content": original.assistant_text},
                    ]:
                        raise ValueError("Checkpoint content changed")
                if not db.conn.execute("SELECT 1 FROM trees WHERE tree_id=?", (row[2],)).fetchone():
                    raise ValueError("Retrieval projection is missing")
                return tree, row[2], ()
            except (ValueError, KeyError, TypeError):
                return (
                    create_conversation_index(self.session.id, executor=self.executor),
                    None,
                    ("checkpoint_invalid: rebuild from committed turns",),
                )
        return create_conversation_index(self.session.id, executor=self.executor), None, ()

    def _state(self, tree, tree_id, diagnostics=()) -> MemoryState:
        count = len(tree.turns)
        return MemoryState(
            self.policy_key,
            tree_id,
            count,
            len(self.prefix) - count,
            fingerprint(self.prefix[:count]),
            tuple(diagnostics),
        )

    def state(self) -> MemoryState:
        self.check_current()
        with open_runtime(self.kb_dir) as db:
            _schema(db)
            return self._state(*self._restore(db))

    def catch_up(self) -> MemoryState:
        self.check_current()
        with open_runtime(self.kb_dir) as db:
            _schema(db)
            tree, tree_id, diagnostics = self._restore(db)
        for turn in self.session.turns[len(tree.turns) :]:
            try:
                self.check_current()
                # add_exchange itself restores a candidate before changing the live tree.
                # No network request runs inside a SQLite write transaction.
                tree.add_exchange(turn.turn_id, turn.user_text, turn.assistant_text)
                snapshot = tree.snapshot_state()
                exported = tree.export_retrieval_tree(content_roles=CONTENT_ROLES)
                self.check_current()
                with open_runtime(self.kb_dir) as db, db.transaction() as connection:
                    _schema(db)
                    self.check_current()
                    new_tree = ConDB(storage=db).store(exported, format="chatindex")
                    connection.execute(
                        "INSERT OR REPLACE INTO okb_conversation_checkpoints "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            self.session.id,
                            self.policy_key,
                            json.dumps(self.prefix[: len(tree.turns)]),
                            json.dumps(snapshot, ensure_ascii=False),
                            new_tree,
                        ),
                    )
                    if tree_id:
                        db.delete_tree(tree_id)
                    self.check_current()
                tree_id = new_tree
            except Exception as error:
                raise_if_terminal(error)
                if isinstance(error, FileNotFoundError) or "changed during context" in str(error):
                    raise
                # Failed SQL commit or append must restore the last durable prefix.
                with open_runtime(self.kb_dir) as db:
                    _schema(db)
                    tree, tree_id, _ = self._restore(db)
                return self._state(
                    tree, tree_id, (*diagnostics, f"index_lag: {type(error).__name__}")
                )
        return self._state(tree, tree_id, diagnostics)


def delete_memory(kb_dir: Path, session_id: str) -> None:
    """Managed deletion already holds both leases and has committed the tombstone."""
    if not runtime_database(kb_dir).exists():
        return
    with open_runtime(kb_dir) as db, db.transaction() as connection:
        _schema(db)
        trees = connection.execute(
            "SELECT tree_id FROM okb_conversation_checkpoints WHERE session_id=?",
            (session_id,),
        ).fetchall()
        for (identity,) in trees:
            db.delete_tree(identity)
