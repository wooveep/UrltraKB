"""Durable accepted questions while the knowledge base is busy.

The private desktop outbox never replays model work. After a stopped task or a
restart it moves unconfirmed submissions into the normal conversation timeline.
Stable attempt IDs make recovery idempotent across the two atomic file writes.
"""

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from time import time_ns
from uuid import uuid4

from ultrakb.lifecycle import KnowledgeBaseRemoved, current_generation, expected_generation
from ultrakb.locks import atomic_write_json, file_write_lock


@dataclass(frozen=True)
class Submission:
    id: str
    order: int


class ChatOutbox:
    def __init__(self, directory: Path):
        self.directory = directory

    def _directory(self, root):
        return self.directory / sha256(str(root).encode()).hexdigest()

    def accept(
        self,
        root,
        identity,
        message,
        *,
        new,
        after_turn=0,
        view_id="legacy",
        database_identity=None,
    ):
        from ultrakb.storage.database import open_database

        with open_database(root, expected=database_identity, read_only=True) as db:
            database_identity = db.identity
        record = {
            "id": uuid4().hex,
            "database_identity": asdict(database_identity),
            "session_id": identity,
            "message": message,
            "new": new,
            "generation": current_generation(root),
            "after_turn": after_turn,
            "view_id": view_id,
        }
        directory = self._directory(root)
        with file_write_lock(directory / ".lock"):
            counter = directory / "sequence"
            sequence = json.loads(counter.read_text()) if counter.exists() else 0
            if type(sequence) is not int or sequence < 0:
                raise ValueError("Invalid saved submission sequence")
            record["sequence"] = max(sequence + 1, time_ns())
            atomic_write_json(counter, record["sequence"])
            atomic_write_json(directory / (record["id"] + ".json"), record)
        return Submission(record["id"], record["sequence"])

    def discard(self, root, identity):
        directory = self._directory(root)
        with file_write_lock(directory / ".lock"):
            (directory / (identity + ".json")).unlink(missing_ok=True)

    def recover(self, root, *, exclude=(), on_wait=None):
        from ultrakb.application.sessions import retain_pending_conversation
        from ultrakb.storage.database import DatabaseIdentity, open_database

        directory = self._directory(root)
        if not directory.exists():
            return
        with file_write_lock(directory / ".lock", on_wait=on_wait):
            records = []
            for path in sorted(directory.glob("*.json")):
                if path.stem in exclude:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(record, dict)
                    or record.get("id") != path.stem
                    or any(
                        not isinstance(record.get(k), str)
                        for k in ("session_id", "message", "generation")
                    )
                    or type(record.get("new")) is not bool
                    or type(record.get("sequence")) is not int
                    or type(record.get("after_turn")) is not int
                    or record["after_turn"] < 0
                    or not isinstance(record.get("database_identity"), dict)
                ):
                    raise ValueError("Invalid saved chat submission")
                records.append(record)
        # Waiting for a conversation never holds the desktop's admission lock.
        for record in sorted(records, key=lambda entry: entry["sequence"]):
            try:
                with expected_generation(root, record["generation"]):
                    pass
            except KnowledgeBaseRemoved:
                self.discard(root, record["id"])
                continue
            expected = DatabaseIdentity(**record["database_identity"])
            with open_database(root, read_only=True) as db:
                current = db.identity
            if current != expected:
                self.discard(root, record["id"])
                continue
            retain_pending_conversation(
                root,
                record["session_id"],
                record["id"],
                record["message"],
                identity=expected,
                new=record["new"],
                after_turn=record["after_turn"],
                order=record["sequence"],
                view_id=record.get("view_id", "legacy"),
                on_wait=on_wait,
            )
            self.discard(root, record["id"])
