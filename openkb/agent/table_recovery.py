"""Revalidate unchanged prose from the deployed, pre-table-object extractor.

Only this exact deployed contract is accepted. Its whole original batch remains
part of the key, even when new native table units replace some of its cell units.
No old table interpretations or publication decisions are adopted.
"""

from openkb.sources import content_id

PRE_TABLE_MODULES = {
    "evidence_facts": "213c786212bd7941eb6ee4985f2c15657a855eda90a926339645c8ed346516d9",
    "evidence_fact_cache": "0e8f4cf79ca111b2f1cbccb3e8662eec2e6f48a8d0b396882672bacc5791ea4f",
    "evidence_units": "5d74b6db0bf8e3fdb82bfc52d9da1305171c9114ffd01bb9ccb2cda626800dd2",
    "evidence_retry": "a6401ea17aa8bfa020b0869bc0c60cdd91b0627be6850c0546b54de4548f6be8",
    "evidence_coverage": "41d9efc8652192d5f26802165496c015f15b7bc5d9edef4e057291ce694e4885",
    "evidence_quotes": "0089615e08a417e20d31af865abe364d7c510f8ca915b9d8cfb17de993625baf",
}


def restore_prose(cache, key, record):
    from openkb.agent.evidence_retry import ResponseIncomplete

    contract = record.get("contract", {})
    payload = contract.get("payload", {})
    units = payload.get("units")
    if not isinstance(units, list) or not units:
        return
    expected = cache.checkpoints._key_record(cache.system, payload)
    expected.update(
        message_format=PRE_TABLE_MODULES["evidence_units"],
        stage_implementation=PRE_TABLE_MODULES,
    )
    if content_id(expected) != key:
        return
    originals = {
        unit.get("id"): unit
        for unit in units
        if isinstance(unit, dict) and isinstance(unit.get("id"), str)
    }
    rows = record.get("value", {}).get("units", [])
    if not isinstance(rows, list):
        return
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    if (
        len(ids) != len(rows)
        or any(not isinstance(uid, str) for uid in ids)
        or len(ids) != len(set(ids))
    ):
        return
    for row in rows:
        unit = cache.units.get(row["id"])
        if (
            unit is None
            or "table_object" in unit
            or originals.get(unit["id"]) != unit
            or not row.get("facts")
        ):
            continue
        try:
            cache.validate(unit, row)
        except ResponseIncomplete:
            continue
        cache.rows.setdefault(unit["id"], row)
