"""Durable index-stage responses and validated trees, outside publication rollback."""

import hashlib
import json
from pathlib import Path

from ultrakb.locks import atomic_record_lock, atomic_write_json
from ultrakb.state import HashRegistry


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class IndexStages:
    def __init__(self, kb_dir: Path, source: Path, *, identity: dict):
        self.identity = {
            "version": 1,
            "source_digest": HashRegistry.hash_file(source),
            "source_name": source.name,
            "processing": identity,
        }
        self.directory = kb_dir / ".ultrakb/index-stages" / digest(self.identity)
        self.used: set[str] = set()
        self.reused = 0

    def load(self, key, *, once=False):
        name = digest(key)
        if once and name in self.used:
            return None
        path = self.directory / (name + ".json")
        if not path.exists():
            return None
        with atomic_record_lock(self.directory / "checkpoint.lock"):
            record = json.loads(path.read_text())
            if (
                record.get("identity") != self.identity
                or record.get("key") != key
                or record.get("digest") != digest(record.get("result"))
            ):
                raise ValueError("Index stage checkpoint is invalid")
            self.used.add(name)
            self.reused += 1
            return record["result"]

    def save(self, key, result):
        name = digest(key)
        with atomic_record_lock(self.directory / "checkpoint.lock"):
            atomic_write_json(
                self.directory / (name + ".json"),
                {"identity": self.identity, "key": key, "digest": digest(result), "result": result},
            )
            self.used.add(name)
