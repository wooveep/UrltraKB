"""Withdrawal of admitted sources leaves all published evidence available as history."""

import hashlib
import json
from pathlib import Path

from openkb.artifact_references import list_artifact_references
from openkb.knowledge_scope import KnowledgeScope, live_scope
from openkb.mutation import mutation_scope
from openkb.source_catalog import list_sources, record_path, write_record
from openkb.source_changes import exclude_source_inputs, source_change_heads, source_view
from openkb.unit_publication import list_source_units, read_head


def unmapped_removal_matches(kb_dir: Path, registry, identifier: str):
    """A catalog owner always supersedes its legacy hash, including after withdrawal."""
    from openkb.application.removal import _resolve_doc_identifier

    mapped = {source.legacy_hash for source in list_sources(kb_dir) if source.legacy_hash}
    return [(h, m) for h, m in _resolve_doc_identifier(registry, identifier) if h not in mapped]


def preview_source_removal(kb_dir: Path, identifier: str, scope: KnowledgeScope | None):
    from openkb.application.removal import RemovalPreview, RemoveAction, RemovePlan

    matches = [
        source
        for source in list_sources(kb_dir)
        if not source.removed
        and identifier in {source.source_id, source.doc_name, source.name, source.legacy_hash}
    ]
    if scope:
        matches = [
            source
            for source in matches
            if any(
                unit.unit_id in read_head(kb_dir, scope.view_id).inputs
                for unit in list_source_units(kb_dir, source.source_id)
            )
            or source_view(kb_dir, source) == scope.view_id
        ]
    if not matches:
        return None
    if len(matches) > 1:
        return RemovalPreview("multiple", candidates=tuple(s.source_id for s in matches))
    source = matches[0]
    view = scope or live_scope(kb_dir, source_view(kb_dir, source))
    changed = exclude_source_inputs(kb_dir, source, "withdrawn").model_copy(
        update={"removed": True, "target_generation": source.target_generation + 1}
    )
    heads = source_change_heads(kb_dir, changed, "withdrawn")
    version = hashlib.sha256(
        json.dumps(
            [
                source.model_dump(mode="json"),
                {str(p): h.model_dump(mode="json") for p, h in heads.items()},
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    references = list_artifact_references(kb_dir)
    plan = RemovePlan(
        name=source.name,
        doc_name=source.doc_name,
        doc_type="admitted",
        file_hash=source.source_id,
        actions=[
            RemoveAction("WITHDRAW", source.name),
            RemoveAction("RETAIN", "Historical evidence and referenced assets"),
        ],
        concept_deletes=[],
        entity_deletes=[],
        raw_path=None,
        cleanup_pageindex=False,
        pageindex_doc_id=None,
        summary_path=view.wiki_dir / "summaries" / f"{source.doc_name}.md",
        source_md=view.wiki_dir / "sources" / f"{source.doc_name}.md",
        source_json=view.wiki_dir / "sources" / f"{source.doc_name}.json",
        images_dir=view.wiki_dir / "sources/images" / source.doc_name,
    )
    for unit in list_source_units(kb_dir, source.source_id):
        if unit.key != "body":
            plan.actions.append(
                RemoveAction("WITHDRAW", f"Worksheet {unit.name or unit.doc_name} ({unit.unit_id})")
            )
    if not references.complete:
        plan.actions.append(
            RemoveAction("RETAIN", "Reference inventory incomplete; physical cleanup deferred")
        )
    return RemovalPreview("ready", version, plan)


def withdraw_source(kb_dir: Path, preview):
    from openkb.application.removal import RemovalOutcome, RemoveResult
    from openkb.catalog_schema import CatalogSchema, catalog_schema_path
    from openkb.source_catalog import read_source

    source = read_source(kb_dir, preview.plan.file_hash)
    changed = exclude_source_inputs(kb_dir, source, "withdrawn").model_copy(
        update={"removed": True, "target_generation": source.target_generation + 1}
    )
    records = {
        catalog_schema_path(kb_dir): CatalogSchema(),
        record_path(kb_dir, "sources", source.source_id): changed,
        **source_change_heads(kb_dir, changed, "withdrawn"),
    }
    with mutation_scope(kb_dir, list(records), operation="withdraw-source"):
        for path, record in records.items():
            write_record(path, record)
    references = list_artifact_references(kb_dir)
    result = RemoveResult(
        status="removed",
        name=source.name,
        doc_name=source.doc_name,
        actions=preview.plan.actions,
        concepts_deleted=[],
        entities_deleted=[],
        lint_files_changed=0,
        lint_ghosts_removed=0,
        pageindex_message=None,
        pageindex_error=None,
        message="Source withdrawn; dependent knowledge needs refresh. "
        "Historical evidence retained.",
    )
    return RemovalOutcome(
        "removed",
        preview,
        result,
        tuple(
            p.relative_to(kb_dir).as_posix()
            for p in sorted(references.paths)
            if p.is_relative_to(kb_dir)
        ),
        ("physical_cleanup_deferred",) if not references.complete else (),
    )
