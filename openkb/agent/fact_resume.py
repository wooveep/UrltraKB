"""Rebind source-local facts when OCR changes only part of an immutable parse."""

from openkb.agent.evidence_retry import ResponseIncomplete
from openkb.evidence import ParseStore
from openkb.processing import processing_checkpoint
from openkb.sources import content_id, read_object, valid_id

# This deployed implementation has the same extraction contract; only recovery changed.
PREVIOUS_CACHE = "44430bf332be4dddc1dc276ce8009ca9d43b79de39c8288a8986995725e0fed5"


def _compatible(cache, key, contract):
    from openkb.agent.table_recovery import PRE_TABLE_MODULES

    cp = cache.checkpoints
    expected = cp._key_record(cache.system, contract["payload"])
    expected["input"] = {**cp.input, "parse": contract["input"]["parse"]}
    if expected == contract:
        return "current"
    expected["stage_implementation"].pop("fact_resume")
    expected["stage_implementation"]["evidence_fact_cache"] = PREVIOUS_CACHE
    if expected == contract:
        return "current"
    expected.update(
        message_format=PRE_TABLE_MODULES["evidence_units"],
        stage_implementation=PRE_TABLE_MODULES,
    )
    return "pre_table" if expected == contract else None


def _rebind(value, before, after):
    """Change only structured evidence references, never text or arbitrary IDs."""
    if isinstance(value, list):
        return [_rebind(item, before, after) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _rebind(item, before, after) for key, item in value.items()}
    if {"source_id", "version_id", "parse_id", "block_id"} <= set(value):
        if (
            value["source_id"] != before["source"]
            or value["version_id"] != before["version"]
            or value["parse_id"] != before["parse"]
        ):
            raise ValueError("Foreign evidence cannot be rebound")
        result["parse_id"] = after["parse"]
    return result


def restore_unchanged(cache, keys):
    cp = cache.checkpoints
    parser = ParseStore(cp.store.kb_dir)
    parsed = parser.load(cp.input["parse"])
    compatible_parses = {parsed.id: True}
    recovered = set()
    for record, mode in _records(cache, keys):
        processing_checkpoint("facts")
        try:
            before = record["input"]
            units = record["contract"]["payload"]["units"]
            rows = record["value"]["units"]
            previous = valid_id(before["parse"])
            if previous not in compatible_parses:
                old = parser.load(previous)
                compatible_parses[previous] = (
                    old.input_key == parsed.input_key and old.profile == parsed.profile
                )
            if not compatible_parses[previous]:
                continue
            mapped = {}
            for unit in units:
                if content_id({k: v for k, v in unit.items() if k != "id"}) != unit["id"]:
                    continue
                rebound = _rebind({k: v for k, v in unit.items() if k != "id"}, before, cp.input)
                rebound = {"id": content_id(rebound), **rebound}
                current = cache.units.get(rebound["id"])
                if current == rebound and "table_object" not in current:
                    mapped[unit["id"]] = current
            ids = [row.get("id") for row in rows if isinstance(row, dict)]
            if (
                len(ids) != len(rows)
                or any(not isinstance(uid, str) for uid in ids)
                or len(ids) != len(set(ids))
            ):
                continue
            if not set(ids) <= {unit["id"] for unit in units}:
                continue
            for row in rows:
                current = mapped.get(row["id"])
                prefer_original = mode == "current" and previous != parsed.id
                if (
                    current is None
                    or current["id"] in recovered
                    or (current["id"] in cache.rows and not prefer_original)
                ):
                    continue
                # Empty rows can depend on another member of their original
                # batch. They survive only when that complete batch is unchanged.
                if not row.get("facts") and (mode == "pre_table" or len(mapped) != len(units)):
                    continue
                rebound_row = {**row, "id": current["id"]}
                try:
                    cache.validate(current, rebound_row)
                except ResponseIncomplete:
                    continue
                cache.rows[current["id"]] = rebound_row
                recovered.add(current["id"])
        except (ValueError, KeyError, TypeError, FileNotFoundError):
            continue  # A damaged/retired checkpoint cannot authorize source facts.


def _records(cache, keys):
    cp = cache.checkpoints
    records = []
    for key in keys:
        processing_checkpoint("facts")
        try:
            record = read_object(cp.store.owned_path(cp.root / f"{valid_id(key)}.json"))
            before = record.get("input", {})
            if not isinstance(before, dict) or {**before, "parse": cp.input["parse"]} != cp.input:
                continue
            contract = record.get("contract", {})
            if not isinstance(contract, dict):
                continue
            payload = contract.get("payload", {})
            if not isinstance(payload, dict):
                continue
            units = payload.get("units")
            value = record.get("value", {})
            rows = value.get("units") if isinstance(value, dict) else None
            if (
                payload.get("stage") != "facts"
                or not isinstance(units, list)
                or not units
                or any(
                    not isinstance(unit, dict) or not isinstance(unit.get("id"), str)
                    for unit in units
                )
                or not isinstance(rows, list)
                or record.get("key") != key
                or contract.get("input") != before
                or content_id(contract) != key
                or content_id(value) != record.get("value_digest")
            ):
                continue
            mode = _compatible(cache, key, contract)
            if mode is None:
                continue
            records.append((record, mode))
        except (ValueError, KeyError, TypeError, FileNotFoundError):
            continue
    # Prefer the original valid progress with the current extraction rules.
    # A superseded pre-table extractor must not replace a newer result merely
    # because its content hash sorts first in the checkpoint index.
    return sorted(
        records,
        key=lambda item: (item[1] == "pre_table", item[0]["input"]["parse"] == cp.input["parse"]),
    )
