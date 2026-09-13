"""Retain reserved requests before transport, including owners lost without an outcome."""

from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

from openkb.locks import atomic_write_json
from openkb.processing import validate_usage
from openkb.sources import read_object, valid_id

_OWNER: ContextVar[str | None] = ContextVar("source_request_owner", default=None)


@contextmanager
def journal_source_requests(store, source, budget):
    identity = budget.measurement.identity
    if _OWNER.get() == identity:
        yield  # Embedded sources consume the enclosing document's allowance.
        return
    token = _OWNER.set(identity)
    path = store.owned_path(store.root / "request-runs" / source.source_id / f"{identity}.json")
    previous = budget.on_observation

    def observe(value):
        previous(value)
        atomic_write_json(
            path,
            {
                "id": identity,
                "source": source.source_id,
                "version": source.id,
                "usage": {
                    "observable_attempts": value.attempts,
                    "charged_tokens": value.charged_tokens,
                    "unknown_usage": sum(row["usage"] is None for row in value.observations),
                    "elapsed_seconds": monotonic() - value.started,
                    "requests": list(value.observations),
                },
            },
        )

    budget.on_observation = observe
    try:
        yield
    finally:
        try:
            with budget.lock:
                if budget.observations:
                    observe(budget)
        finally:
            budget.on_observation = previous
            _OWNER.reset(token)


def unreported_source_usage(store, source_id, reported_ids):
    """Only add requests absent from durable outcomes; never add a second receipt copy."""
    root = store.owned_path(store.root / "request-runs" / valid_id(source_id, source=True))
    totals = {
        "runs": 0,
        "observable_attempts": 0,
        "charged_tokens": 0,
        "unknown_usage": 0,
        "elapsed_seconds": 0.0,
    }
    pending = []
    for path in root.glob("*.json"):
        record = read_object(store.owned_path(path))
        if set(record) != {"id", "source", "version", "usage"}:
            raise ValueError("Invalid source request journal")
        identity = valid_id(record["id"], source=True)
        if path.stem != identity or record["source"] != source_id:
            raise ValueError("Source request journal identity mismatch")
        # Cost history outlives unreferenced original versions removed by GC.
        valid_id(record["version"])
        usage = record["usage"]
        validate_usage(usage)
        seen = set()
        missing = []
        for row in usage["requests"]:
            request_id = f"{identity}:{row['attempt']}"
            if request_id in seen:
                raise ValueError("Duplicate source request journal identity")
            seen.add(request_id)
            if request_id not in reported_ids:
                missing.append({"id": request_id, **row})
        if not missing:
            continue
        pending.extend(missing)
        totals["observable_attempts"] += len(missing)
        totals["unknown_usage"] += sum(row["usage"] is None for row in missing)
        totals["charged_tokens"] += sum(
            sum(row["usage"].values()) if row["usage"] is not None else row["reserved_tokens"]
            for row in missing
        )
        if not (seen & reported_ids):
            totals["runs"] += 1
            totals["elapsed_seconds"] += usage["elapsed_seconds"]
    return totals, pending
