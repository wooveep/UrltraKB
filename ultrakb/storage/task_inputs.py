"""Retained input descriptions for bounded task stage pages and drilldown."""

from ultrakb.task_records import TaskInput

from .catalog_store import CatalogStore


def describe_stage_input(db, stage):
    """Read only the retained manifest; original source bytes are not opened."""
    if stage.source_revision_id is None and stage.unit_revision_id is None:
        return stage
    try:
        unit = (
            CatalogStore._unit_revision(db, stage.unit_revision_id)
            if stage.unit_revision_id is not None
            else None
        )
        revision = CatalogStore._revision(db, stage.source_revision_id or unit.source_revision_id)
        if unit is not None and unit.source_revision_id != revision.revision_id:
            raise ValueError("Stage source and processing unit revisions disagree")
        original = next(file for file in revision.files if file.kind == "main")
        fingerprint = (
            db.connection.execute(
                "SELECT fingerprint FROM ukb_processing_versions WHERE processing_id = ?",
                (unit.processing_id,),
            ).fetchone()[0]
            if unit is not None
            else None
        )
        return stage.model_copy(
            update={
                "input": TaskInput(
                    source_id=revision.source_id,
                    source_revision_id=revision.revision_id,
                    source_ordinal=revision.ordinal,
                    name=original.path,
                    original_digest=original.digest,
                    original_bytes=original.byte_count,
                    unit_revision_id=unit.unit_revision_id if unit else None,
                    unit_id=unit.unit_id if unit else None,
                    unit_key=unit.key if unit else None,
                    unit_label=unit.label if unit else None,
                    document_name=unit.doc_name if unit else None,
                    processing_fingerprint=fingerprint,
                )
            }
        )
    except (KeyError, ValueError, OSError):
        return stage.model_copy(update={"input_unavailable_reason": "retained_input_unavailable"})


def describe_stage_inputs(db, stages):
    """Resolve each retained input once within this bounded read snapshot."""
    inputs, result = {}, []
    for stage in stages:
        key = (stage.source_revision_id, stage.unit_revision_id)
        if key not in inputs:
            described = describe_stage_input(db, stage)
            inputs[key] = (described.input, described.input_unavailable_reason)
        original, reason = inputs[key]
        result.append(
            stage.model_copy(update={"input": original, "input_unavailable_reason": reason})
        )
    return tuple(result)
