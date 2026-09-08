"""Durable accepted questions while the knowledge base is busy.

The private desktop outbox never replays model work. After a stopped task or a
restart it moves unconfirmed submissions into the normal conversation timeline.
Stable attempt IDs make recovery idempotent across the two atomic file writes.
"""

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import time_ns
from uuid import uuid4

from openkb.agent.chat_session import ChatSession, deletion_marker, load_session
from openkb.lifecycle import KnowledgeBaseRemoved, current_generation, expected_generation
from openkb.locks import atomic_write_json, file_write_lock, kb_ingest_lock, session_lock


@dataclass(frozen=True)
class Submission:
    id: str
    order: int


class ChatOutbox:
    def __init__(self, directory: Path):
        self.directory = directory

    def _directory(self, root):
        return self.directory / sha256(str(root).encode()).hexdigest()

    def accept(self, root, identity, message, *, new, after_turn=0):
        record = {
            "id": uuid4().hex,
            "session_id": identity,
            "message": message,
            "new": new,
            "generation": current_generation(root),
            "after_turn": after_turn,
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
                ):
                    raise ValueError("Invalid saved chat submission")
                records.append((record, path))
            for record, path in sorted(records, key=lambda entry: entry[0]["sequence"]):
                identity = record["session_id"]
                try:
                    with expected_generation(root, record["generation"]):
                        pass
                except KnowledgeBaseRemoved:
                    # A deleted/recreated KB owns a different history. Stale
                    # submissions must neither repopulate it nor block newer ones.
                    path.unlink()
                    continue
                with session_lock(root, identity, on_wait=on_wait):
                    with expected_generation(root, record["generation"]):
                        with kb_ingest_lock(root / ".openkb", on_wait=on_wait):
                            if deletion_marker(root, identity).exists():
                                path.unlink()
                                continue
                            try:
                                session = load_session(root, identity)
                            except FileNotFoundError:
                                if not record["new"]:
                                    # An explicitly deleted existing conversation must stay deleted.
                                    path.unlink()
                                    continue
                                session = ChatSession.new(root, "", "", identity=identity)
                            if record["id"] not in session.completed_attempts:
                                session.begin_attempt(
                                    record["message"],
                                    identity=record["id"],
                                    after_turn=min(record["after_turn"], session.turn_count),
                                    submission_order=record["sequence"],
                                )
                            path.unlink()
