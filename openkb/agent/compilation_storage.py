"""Private, disposable compilation bodies; durable checkpoints remain authoritative."""

import threading
import weakref
from collections.abc import MutableMapping
from pathlib import Path
from tempfile import gettempdir

from openkb.locks import atomic_write_json
from openkb.resource_checks import check_disk, json_size, resource_operation
from openkb.runtime.input_store import InputStore
from openkb.sources import content_id, read_object


class CompilationStorage:
    def __init__(self):
        self.directory = InputStore(Path(gettempdir()).resolve() / "openkb-compilation-bodies")
        self._cleanup = weakref.finalize(self, self.directory.cleanup)
        self._tables = {}
        self._lock = threading.RLock()

    def rows(self, name):
        with self._lock:
            if name not in self._tables:
                self._tables[name] = PrivateRows(Path(self.directory.name) / content_id(name))
            return self._tables[name]

    def close(self):
        self._cleanup()


class PrivateRows(MutableMapping):
    """Keep only keys in RAM; each reader owns its decoded row and mutations."""

    def __init__(self, root):
        self.root = root
        self._keys = {}
        self._lock = threading.RLock()

    def __getitem__(self, key):
        with self._lock:
            path = self._keys[key]
            from openkb.resource_budget import check_memory

            check_memory(path.stat().st_size * 6, stage="checkpoint")
            return read_object(path)["value"]

    def __setitem__(self, key, value):
        with self._lock:
            path = self.root / f"{content_id(key)}.json"
            record = {"value": value}
            check_disk(path, json_size(record), stage="checkpoint", operation="private_body")
            with resource_operation("checkpoint", "private_body"):
                atomic_write_json(path, record)
            self._keys[key] = path

    def __delitem__(self, key):
        with self._lock:
            self._keys[key].unlink()
            del self._keys[key]

    def __iter__(self):
        with self._lock:
            return iter(tuple(self._keys))

    def __len__(self):
        with self._lock:
            return len(self._keys)

    def __contains__(self, key):
        with self._lock:
            return key in self._keys


class UnitInventory:
    """Ordered source identities with bodies loaded only for the active batch."""

    def __init__(self, checkpoints, units):
        self.by_id = checkpoints.private_rows("source_units")
        self.order = {}
        self.characters = 0
        self.content_blocks = set()
        self._runs = []
        tables, prose = {}, []
        for number, unit in enumerate(units):
            self.by_id[unit["id"]] = unit
            self.order[unit["id"]] = number
            self.characters += len(unit["text"])
            if unit["kind"] not in {"image", "heading"}:
                self.content_blocks.add(unit["reference"]["block_id"])
            identity = unit.get("table_object", {}).get("id")
            if identity is None:
                prose.append(unit["id"])
                continue
            if prose:
                self._runs.append((None, prose))
                prose = []
            if identity not in tables:
                tables[identity] = []
                self._runs.append((identity, tables[identity]))
            tables[identity].append(unit["id"])
        if prose:
            self._runs.append((None, prose))

    def __iter__(self):
        return iter(self.by_id.values())

    def runs(self):
        for identity, keys in self._runs:
            yield identity, (self.by_id[key] for key in keys)


class FactInventory:
    """Body storage and a compact topic-to-fact index, shared by all stages."""

    def __init__(self, checkpoints):
        self.rows = checkpoints.private_rows("source_facts")
        self.topics = {}
        self.routes = {}

    def append(self, fact):
        if fact["id"] not in self.rows:
            self.rows[fact["id"]] = fact
            self.topics.setdefault(fact["topic"], []).append(fact["id"])
            self.routes[fact["id"]] = {
                key: fact[key]
                for key in ("id", "topic", "scope", "context_evidence")
                if key in fact
            }

    def for_topics(self, topics):
        return [self.rows[uid] for topic in topics for uid in self.topics.get(topic, ())]

    def routing_for_topics(self, topics):
        return [self.routes[uid] for topic in topics for uid in self.topics.get(topic, ())]

    def exclude_blocks(self, blocks):
        for uid in self.rows:
            fact = self.routes[uid]
            if fact["scope"]["block_id"] in blocks:
                del self.rows[uid]
                del self.routes[uid]
                self.topics[fact["topic"]].remove(uid)
                if not self.topics[fact["topic"]]:
                    del self.topics[fact["topic"]]

    def __iter__(self):
        return iter(self.rows.values())

    def __len__(self):
        return len(self.rows)


class CandidateInventory:
    """Completed bodies are private until the existing publication transaction."""

    def __init__(self, checkpoints):
        self.rows = checkpoints.private_rows("verified_candidates")
        self.groups = {}
        self.links = {}

    def append(self, candidate):
        from openkb.agent.answer_citations import _links

        group, content = candidate
        self.rows[group["path"]] = {"content": content}
        self.groups[group["path"]] = group
        self.links[group["path"]] = " ".join(_links(content))

    def content(self, path):
        return self.rows[path]["content"]

    def retain(self, paths):
        for path in list(self.groups):
            if path not in paths:
                del self.groups[path]
                del self.links[path]
                del self.rows[path]
        return self

    def __iter__(self):
        for path, group in self.groups.items():
            yield group, self.content(path)

    def __len__(self):
        return len(self.groups)
