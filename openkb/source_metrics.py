"""Project saved source measurements without measuring or converting inputs on read."""


def unit_metrics(kb_dir, state, actual, doc_name):
    from openkb.ingest_records import UnitRevision
    from openkb.normalization import read_processing, read_retained_source
    from openkb.source_catalog import read_record
    from openkb.source_map import read_source_map

    value = None
    processing = None
    if actual:
        directory, manifest = actual
        if manifest.source_map:
            if manifest.source_map.unit_kind == "page":
                return {"pages": manifest.source_map.unit_count}
            value = read_source_map(directory / "wiki", manifest.source_map, doc_name)
        processing = manifest.processing.model_dump(mode="json") if manifest.processing else None
    elif state:
        target = read_record(kb_dir, "unit-revisions", state.target_revision_id, UnitRevision)
        retained = read_retained_source(
            kb_dir, target.unit_revision_id, target.source_revision_id, doc_name
        )
        value = retained[1] if retained else None
        processing = read_processing(kb_dir, target.unit_revision_id)
    if value:
        return {key: value.get(key) for key in ("pages", "tokens", "characters", "block_count")}
    if processing:
        key = "pages" if processing["measurement_unit"] == "physical_page" else "tokens"
        return {key: processing["measurement_value"]}
    return {}
