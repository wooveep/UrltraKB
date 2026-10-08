"""Preview and resume explicit existing-source identity corrections.

Each source uses the ordinary version-review/publication path. The batch is a
durable sequence of recoverable steps, not a transaction spanning model calls.
"""

import hashlib
import json
import uuid
from pathlib import Path

from openkb.application.products import (
    confirm_product_aliases,
    list_products,
    retire_product_identity,
)
from openkb.application.query_views import list_family_defaults
from openkb.application.version_review import (
    read_version_review,
    resume_version_review,
    review_source_version,
    supplement_version_reviews,
)
from openkb.ingest_records import UnitRevision
from openkb.locks import kb_ingest_lock, kb_read_lock
from openkb.mutation import mutation_scope
from openkb.source_catalog import (
    list_sources,
    read_record,
    read_source,
    read_source_revision,
    record_path,
    write_record,
)
from openkb.source_records import Record, RecordId
from openkb.view_records import KnowledgeView, SourceMetadata, VersionAnnotation


class IdentityRepair(Record):
    repair_id: RecordId
    preview: dict
    source_results: dict
    status: str = "pending"


def preview_identity_repair(
    kb_dir: Path, target_id: str, source_ids: tuple[str, ...], *, aliases: tuple[str, ...] = ()
) -> dict:
    with kb_read_lock(kb_dir / ".openkb"):
        products = {p.product_id: p for p in list_products(kb_dir)}
        target = products.get(target_id)
        if (
            target is None
            or target.retired_into
            or not source_ids
            or len(set(source_ids)) != len(source_ids)
        ):
            raise ValueError("Choose an active target and unique existing source identities")
        rows = []
        for identity in source_ids:
            source = read_source(kb_dir, identity)
            if source.removed or not source.annotation_id:
                raise ValueError("Only current annotated sources can be corrected")
            annotation = read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation)
            if annotation.view_id == "legacy":
                raise ValueError(
                    "Legacy sources require explicit legacy mapping before identity repair"
                )
            view = read_record(kb_dir, "views", annotation.view_id, KnowledgeView)
            rows.append(
                {
                    "source_id": identity,
                    "name": source.name,
                    "source_revision_id": source.target_revision_id,
                    "generation": source.target_generation,
                    "annotation_id": source.annotation_id,
                    "from_product_id": view.product_id,
                    "from_view_id": view.view_id,
                    "family_id": annotation.family_id,
                    "metadata": annotation.metadata.model_dump(mode="json"),
                    "already_target": view.product_id == target_id,
                }
            )
        defaults = [
            {
                "family_id": item["family_id"],
                "purpose": item["purpose"],
                "product_id": item["product_id"],
                "view_id": item["view_id"],
            }
            for item in list_family_defaults(kb_dir)
            if item["product_id"] in {target_id, *(r["from_product_id"] for r in rows)}
        ]
        preview = {
            "target_id": target_id,
            "target_name": target.name,
            "aliases": list(aliases),
            "sources": rows,
            "family_defaults": defaults,
            "default_conflicts": [
                d for d in defaults if d["view_id"] and d["product_id"] != target_id
            ],
        }
        moved_products = {r["from_product_id"] for r in rows} - {None, target_id}
        unselected = []
        for source in list_sources(kb_dir):
            if source.removed or not source.annotation_id or source.source_id in source_ids:
                continue
            annotation = read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation)
            if annotation.view_id != "legacy":
                view = read_record(kb_dir, "views", annotation.view_id, KnowledgeView)
                if view.product_id in moved_products:
                    unselected.append({"source_id": source.source_id, "name": source.name})
        preview["unselected_sources"] = unselected
        preview["fingerprint"] = hashlib.sha256(
            json.dumps(preview, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return preview


def start_identity_repair(kb_dir: Path, preview: dict) -> IdentityRepair:
    with kb_ingest_lock(kb_dir / ".openkb"):
        current = preview_identity_repair(
            kb_dir,
            preview["target_id"],
            tuple(r["source_id"] for r in preview["sources"]),
            aliases=tuple(preview["aliases"]),
        )
        if current != preview:
            raise ValueError("Identity repair preview is stale; inspect a fresh preview")
        if preview["default_conflicts"]:
            raise ValueError("Resolve explicit family defaults before moving their current sources")
        if preview.get("unselected_sources"):
            raise ValueError(
                "Include the remaining active sources before retiring their product identity"
            )
        repair = IdentityRepair(repair_id=uuid.uuid4().hex, preview=preview, source_results={})
        _save(kb_dir, repair)
        return repair


def _save(kb_dir: Path, repair: IdentityRepair) -> None:
    path = record_path(kb_dir, "identity-repairs", repair.repair_id)
    with mutation_scope(kb_dir, [path], operation="record-identity-repair"):
        write_record(path, repair)


def _reprocessed_correction(kb_dir, source, row, target_id):
    """Accept only a completed, explicit reprocessing of the same original."""
    from openkb.unit_publication import list_source_units, read_unit_publication

    current = read_source_revision(kb_dir, source.target_revision_id)
    previous = read_source_revision(kb_dir, row["source_revision_id"])
    if current.reprocessed_from != previous.source_revision_id or current.digest != previous.digest:
        return False
    if not source.annotation_id:
        return False
    annotation = read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation)
    expected = {**row["metadata"], "product": annotation.metadata.product, "product_id": target_id}
    if annotation.metadata.model_dump(mode="json") != expected:
        return False
    units = list_source_units(kb_dir, source.source_id)
    for unit in units:
        revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
        state = read_unit_publication(kb_dir, unit.unit_id, annotation.view_id)
        if (
            revision.source_revision_id != current.source_revision_id
            or state.status not in {"completed", "empty"}
            or state.successful_revision_id != unit.target_revision_id
        ):
            return False
    return bool(units)


def resume_identity_repair(kb_dir: Path, repair_id: str, *, context=None) -> IdentityRepair:
    from openkb.application.execution import ExecutionContext

    context = context or ExecutionContext()
    with kb_read_lock(kb_dir / ".openkb"):
        initial = read_record(kb_dir, "identity-repairs", repair_id, IdentityRepair)
    if initial.status == "completed":
        return initial
    for index in range(len(initial.preview["sources"])):
        with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled):
            repair = read_record(kb_dir, "identity-repairs", repair_id, IdentityRepair)
            rows = repair.preview["sources"]
            row = rows[index]
            identity = row["source_id"]
            if repair.source_results.get(identity, {}).get("status") == "completed":
                continue
            source = read_source(kb_dir, identity)
            if source.target_revision_id != row["source_revision_id"] or source.removed:
                if not source.removed and _reprocessed_correction(
                    kb_dir, source, row, repair.preview["target_id"]
                ):
                    repair.source_results[identity] = {
                        "status": "completed",
                        "reprocessed_revision_id": source.target_revision_id,
                    }
                    _save(kb_dir, repair)
                    continue
                raise ValueError("Repair source changed; inspect and start a new preview")
            if row["already_target"]:
                if (
                    source.annotation_id != row["annotation_id"]
                    or source.target_generation != row["generation"]
                ):
                    raise ValueError("Repair source metadata changed; inspect a fresh preview")
                repair.source_results[identity] = {"status": "completed", "unchanged": True}
                _save(kb_dir, repair)
                continue
            saved_path = record_path(kb_dir, "version-reviews", row["source_revision_id"])
            saved = (
                read_version_review(kb_dir, row["source_revision_id"])
                if saved_path.exists()
                else None
            )
            if (
                saved
                and saved.status == "completed"
                and saved.metadata.product_id == repair.preview["target_id"]
                and saved.annotation_id == source.annotation_id
            ):
                repair.source_results[identity] = {
                    "status": "completed",
                    "review_id": saved.review_id,
                }
                _save(kb_dir, repair)
                continue
            review = review_source_version(kb_dir, identity)
            if review.status != "completed":
                metadata = SourceMetadata.model_validate_json(
                    json.dumps(
                        {
                            **row["metadata"],
                            "product": repair.preview["target_name"],
                            "product_id": repair.preview["target_id"],
                        }
                    )
                )
                review = supplement_version_reviews(kb_dir, {review.review_id: metadata})[0]
                if review.status == "blocked":
                    repair.source_results[identity] = {
                        "status": "blocked",
                        "review_id": review.review_id,
                        "message": review.reason,
                    }
                    _save(kb_dir, repair)
                    continue
            repair.source_results[identity] = {"status": "running", "review_id": review.review_id}
            repair = repair.model_copy(update={"status": "running"})
            _save(kb_dir, repair)
        context.check_stop()
        result = resume_version_review(kb_dir, review.review_id, context=context)
        with kb_ingest_lock(kb_dir / ".openkb"):
            repair = read_record(kb_dir, "identity-repairs", repair_id, IdentityRepair)
            repair.source_results[identity] = {
                "status": "completed" if result.status in {"added", "skipped"} else result.status,
                "review_id": review.review_id,
                "message": result.message,
            }
            _save(kb_dir, repair)
    with kb_ingest_lock(kb_dir / ".openkb"):
        repair = read_record(kb_dir, "identity-repairs", repair_id, IdentityRepair)
        complete = all(
            repair.source_results.get(row["source_id"], {}).get("status") == "completed"
            for row in repair.preview["sources"]
        )
        if complete:
            old_ids = {row["from_product_id"] for row in repair.preview["sources"]} - {
                None,
                repair.preview["target_id"],
            }
            for identity in sorted(old_ids):
                retire_product_identity(kb_dir, identity, repair.preview["target_id"])
            confirm_product_aliases(
                kb_dir, repair.preview["target_id"], tuple(repair.preview["aliases"])
            )
        repair = repair.model_copy(update={"status": "completed" if complete else "partial"})
        _save(kb_dir, repair)
        return repair
