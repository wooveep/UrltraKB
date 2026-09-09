"""Durable navigation attempts; unknown transport usage remains visible after worker loss."""

from __future__ import annotations

import time
import uuid

from openkb.locks import atomic_write_json
from openkb.processing import validate_usage
from openkb.sources import read_object, valid_id


class NavigationRun:
    def __init__(self, store, source, parsed, profile):
        self.store = store
        self.path = store.owned_path(
            store.root / "navigation-runs" / source.source_id / f"{uuid.uuid4().hex}.json"
        )
        self.record = {
            "source": source.source_id,
            "version": source.id,
            "parse": parsed.id,
            "profile": profile,
            "usage": {},
            "accounting_complete": False,
        }
        atomic_write_json(self.path, self.record)

    def observe(self, budget):
        # Called while the budget owns its accounting lock, before transport
        # starts and when its response settles. No prompts or secrets are stored.
        self.record["usage"] = {
            "observable_attempts": budget.attempts,
            "charged_tokens": budget.charged_tokens,
            "unknown_usage": sum(row["usage"] is None for row in budget.observations),
            "elapsed_seconds": time.monotonic() - budget.started,
            "requests": budget.observations,
        }
        atomic_write_json(self.path, self.record)

    def finish(self, budget):
        with budget.lock:
            self.record["accounting_complete"] = True
            self.observe(budget)
        return self.record["usage"]


def navigation_usage(store, source_id):
    root = store.owned_path(store.root / "navigation-runs" / valid_id(source_id, source=True))
    totals = {
        "runs": 0,
        "observable_attempts": 0,
        "charged_tokens": 0,
        "unknown_usage": 0,
        "elapsed_seconds": 0.0,
        "accounting_complete": True,
    }
    for path in root.glob("*.json"):
        record = read_object(store.owned_path(path))
        if record.get("source") != source_id or type(record.get("accounting_complete")) is not bool:
            raise ValueError("Invalid navigation accounting identity")
        validate_usage(record["usage"])
        totals["runs"] += 1
        totals["accounting_complete"] &= record["accounting_complete"]
        for key in ("observable_attempts", "charged_tokens", "unknown_usage", "elapsed_seconds"):
            totals[key] += record["usage"].get(key, 0)
    return totals
