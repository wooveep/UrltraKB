"""Document inventory and original reading from committed source/unit records."""

from __future__ import annotations

import json
from pathlib import Path

from openkb.file_state import contained_paths
from openkb.ingest_records import KnowledgeRevision, UnitPublication, UnitRevision
from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.locks import kb_read_lock
from openkb.source_catalog import list_sources, read_record, read_source_revision
from openkb.source_records import Source
from openkb.unit_publication import list_source_units, read_unit_publication
from openkb.view_records import VersionAnnotation


def source_view_id(kb_dir: Path, source: Source) -> str:
    if source.annotation_id is None:
        return "legacy"
    annotation = read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation)
    if annotation.source_id != source.source_id:
        raise ValueError("Version annotation belongs to another source")
    return annotation.view_id


def _publication(kb_dir: Path, unit_id: str, view_id: str = "legacy") -> UnitPublication | None:
    try:
        return read_unit_publication(kb_dir, unit_id, view_id)
    except FileNotFoundError:
        return None


def _manifest(kb_dir: Path, state: UnitPublication) -> tuple[Path, KnowledgeRevision] | None:
    if state.knowledge_revision_id is None:
        return None
    directory = (
        kb_dir / ".openkb/knowledge" / state.view_id / "revisions" / state.knowledge_revision_id
    )
    contained_paths(kb_dir, [directory])
    manifest = KnowledgeRevision.model_validate_json(
        (directory / "manifest.json").read_text("utf-8")
    )
    if (
        manifest.knowledge_revision_id != state.knowledge_revision_id
        or manifest.view_id != state.view_id
        or manifest.unit_revision_id != state.successful_revision_id
    ):
        raise ValueError("Knowledge revision does not match its publication")
    return directory, manifest


def _historical_manifest(
    kb_dir: Path, state: UnitPublication, source_revision_id: str
) -> tuple[Path, KnowledgeRevision] | None:
    """Follow committed ancestry, never substitute today's normalized source."""
    identity = state.knowledge_revision_id
    visited = set()
    while identity:
        if identity in visited:
            raise ValueError("Knowledge revision ancestry contains a cycle")
        visited.add(identity)
        directory = kb_dir / ".openkb/knowledge" / state.view_id / "revisions" / identity
        contained_paths(kb_dir, [directory])
        manifest = KnowledgeRevision.model_validate_json(
            (directory / "manifest.json").read_text("utf-8")
        )
        if manifest.knowledge_revision_id != identity or manifest.view_id != state.view_id:
            raise ValueError("Knowledge revision does not match its location")
        if manifest.unit_revision_id:
            used = read_record(kb_dir, "unit-revisions", manifest.unit_revision_id, UnitRevision)
            if used.unit_id == state.unit_id and used.source_revision_id == source_revision_id:
                return directory, manifest
        identity = manifest.base_revision_id
    return None


def _snapshot_source(kb_dir: Path, scope: KnowledgeScope, unit_id: str):
    """Find the input used by the selected immutable snapshot, never today's head."""
    relative = scope.wiki_dir.relative_to(kb_dir).parts
    if len(relative) != 6 or relative[3] != "revisions":
        return None
    directory = scope.wiki_dir.parent
    manifest = KnowledgeRevision.model_validate_json((directory / "manifest.json").read_text())
    if manifest.view_id != scope.view_id or manifest.knowledge_revision_id != directory.name:
        raise ValueError("Knowledge snapshot belongs to another view")
    matching = [
        identity
        for identity in manifest.input_revisions
        if read_record(kb_dir, "unit-revisions", identity, UnitRevision).unit_id == unit_id
    ]
    if len(matching) > 1:
        raise ValueError("Snapshot contains multiple revisions of one unit")
    if not matching:
        return None
    seen = set()
    while manifest.knowledge_revision_id not in seen:
        seen.add(manifest.knowledge_revision_id)
        if manifest.unit_revision_id == matching[0]:
            return directory, manifest
        if manifest.base_revision_id is None:
            return None
        directory = (
            kb_dir / ".openkb/knowledge" / scope.view_id / "revisions" / manifest.base_revision_id
        )
        contained_paths(kb_dir, [directory])
        manifest = KnowledgeRevision.model_validate_json((directory / "manifest.json").read_text())
        if manifest.view_id != scope.view_id or manifest.knowledge_revision_id != directory.name:
            raise ValueError("Knowledge snapshot ancestry belongs to another view")
    raise ValueError("Knowledge revision ancestry contains a cycle")


def processing_details(kb_dir: Path, state, manifest) -> dict:
    """Keep the displayed body's policy distinct from its pending/failed replacement."""
    from openkb.normalization import read_processing

    target = read_processing(kb_dir, state.target_revision_id) if state else None
    if manifest:
        processing = manifest.processing.model_dump(mode="json") if manifest.processing else None
        length, mode = manifest.length_class, manifest.execution_mode
    else:
        processing = target
        length, mode = (target or {}).get("length_class"), (target or {}).get("execution_mode")
    return {
        "length_class": length,
        "execution_mode": mode,
        "processing": processing,
        "target_processing": target,
    }


def source_inventory(kb_dir: Path, *, scope: KnowledgeScope | None = None) -> list[dict]:
    root = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(root, scope)
    with kb_read_lock(root / ".openkb"):
        documents = []
        for source in list_sources(root):
            if source.removed and not (scope and scope.read_only):
                continue
            revision = read_source_revision(root, source.target_revision_id)
            units = list_source_units(root, source.source_id)
            view_id = scope.view_id if scope else source_view_id(root, source)
            if scope and scope.read_only:
                units = tuple(unit for unit in units if _snapshot_source(root, scope, unit.unit_id))
            elif scope and source_view_id(root, source) != view_id:
                units = tuple(unit for unit in units if _publication(root, unit.unit_id, view_id))
            states = [
                state for unit in units if (state := _publication(root, unit.unit_id, view_id))
            ]
            if scope and not states and source_view_id(root, source) != view_id:
                continue
            actual = _manifest(root, states[0]) if states else None
            if scope and scope.read_only:
                actual = _snapshot_source(root, scope, units[0].unit_id) if units else None
                if actual is None:
                    continue
            annotation_id = source.annotation_id
            family_id = source.family_id
            if (
                scope
                and (actual or states)
                and not (
                    len(units) > 1
                    and not scope.read_only
                    and source_view_id(root, source) == view_id
                )
            ):
                input_id = actual[1].unit_revision_id if actual else states[0].target_revision_id
                if input_id is None:
                    raise ValueError("Published source is missing its input revision")
                used = read_record(root, "unit-revisions", input_id, UnitRevision)
                revision = read_source_revision(root, used.source_revision_id)
                annotation_id = used.annotation_id
                annotation = (
                    read_record(root, "annotations", annotation_id, VersionAnnotation)
                    if annotation_id
                    else None
                )
                family_id = annotation.family_id if annotation else None
                if scope.read_only and actual:
                    states = [
                        UnitPublication.model_validate_json(
                            (actual[0] / "publication.json").read_text()
                        )
                    ]
            from openkb.source_changes import source_validity
            from openkb.workbooks.catalog import inventory_failure

            inventory_error = (
                inventory_failure(
                    root,
                    source.target_revision_id
                    if source_view_id(root, source) == view_id
                    else revision.source_revision_id,
                )
                if not (scope and scope.read_only)
                else None
            )

            measurement = {}
            if (
                actual
                and actual[1].source_map
                and actual[1].source_map.unit_kind in {"text", "block"}
            ):
                from openkb.source_map import read_source_map

                text = read_source_map(actual[0] / "wiki", actual[1].source_map, units[0].doc_name)
                measurement = {key: text[key] for key in ("tokens", "characters", "block_count")}
            elif not actual and states:
                from openkb.normalization import read_retained_source

                retained = read_retained_source(
                    root,
                    states[0].target_revision_id,
                    revision.source_revision_id,
                    units[0].doc_name,
                )
                if retained and retained[1].get("unit_kind") in {"text", "block"}:
                    measurement = {
                        key: retained[1][key] for key in ("tokens", "characters", "block_count")
                    }
            documents.append(
                {
                    **(measurement if len(units) == 1 else {}),
                    "hash": source.source_id,
                    "source_generation": source.target_generation,
                    "validity": source_validity(
                        root,
                        source,
                        view_id,
                        revision.source_revision_id,
                        historical=bool(scope and scope.read_only),
                        unit_revision_id=actual[1].unit_revision_id if actual else None,
                    ),
                    "source_id": source.source_id,
                    "legacy_hash": source.legacy_hash,
                    "source_revision_id": revision.source_revision_id,
                    "view_id": view_id,
                    "annotation_id": annotation_id,
                    "family_id": family_id,
                    "name": source.name,
                    "type": revision.source_format,
                    **processing_details(
                        root, states[0] if states else None, actual[1] if actual else None
                    ),
                    **(
                        {
                            "length_class": None,
                            "execution_mode": None,
                            "processing": None,
                            "target_processing": None,
                        }
                        if len(units) > 1
                        else {}
                    ),
                    "doc_name": source.doc_name,
                    "unit_count": len(units),
                    "display_type": "pageindex"
                    if actual and actual[1].execution_mode == "segmented"
                    else "short",
                    "status": "failed" if inventory_error else source_status(states),
                    "units": unit_inventory(root, units, view_id, scope=scope),
                    "message": inventory_error or (states[0].message if states else None),
                    "pages": actual[1].source_map.unit_count
                    if actual and actual[1].source_map and actual[1].source_map.unit_kind == "page"
                    else None,
                    "original_path": revision.original,
                }
            )
        return documents


def source_status(states):
    statuses = {state.status for state in states}
    if not statuses:
        return "admitted"
    if len(statuses) == 1:
        return next(iter(statuses))
    finished = {"completed", "empty", "retired"}
    if statuses <= finished:
        return "completed"
    return "partial" if statuses & finished else "failed" if "failed" in statuses else "blocked"


def unit_inventory(kb_dir, units, view_id, *, scope=None):
    from openkb.source_metrics import unit_metrics
    from openkb.workbooks.catalog import unit_name, unit_sheet

    result = []
    for unit in units:
        state = _publication(kb_dir, unit.unit_id, view_id)
        actual = _manifest(kb_dir, state) if state else None
        if scope and scope.read_only:
            actual = _snapshot_source(kb_dir, scope, unit.unit_id)
            if actual is None:
                continue
            state = UnitPublication.model_validate_json(
                (actual[0] / "publication.json").read_text()
            )
        elif scope and state is None:
            continue
        target = read_record(
            kb_dir,
            "unit-revisions",
            state.target_revision_id if state else unit.target_revision_id,
            UnitRevision,
        )
        used = (
            read_record(kb_dir, "unit-revisions", state.successful_revision_id, UnitRevision)
            if state and state.successful_revision_id
            else None
        )
        result.append(
            {
                **(state.model_dump(mode="json") if state else {"status": "pending"}),
                **processing_details(kb_dir, state, actual[1] if actual else None),
                **unit_metrics(kb_dir, state, actual, unit.doc_name),
                "target_source_revision_id": target.source_revision_id,
                "successful_source_revision_id": used.source_revision_id if used else None,
                "unit_id": unit.unit_id,
                "key": unit.key,
                "content_state": (
                    sheet.content_state
                    if (sheet := unit_sheet(kb_dir, unit, target.source_revision_id))
                    else "retired"
                    if state and state.status == "retired"
                    else None
                ),
                "doc_name": unit.doc_name,
                "name": unit_name(
                    kb_dir,
                    unit,
                    state.target_revision_id if state else unit.target_revision_id,
                    unit.doc_name,
                ),
            }
        )
    return result


def read_admitted_source(
    kb_dir: Path,
    identifier: str,
    *,
    source_revision_id: str | None = None,
    unit_id: str | None = None,
    scope: KnowledgeScope | None = None,
    page_range: str | None = None,
    char_range: str | None = None,
    block_range: str | None = None,
) -> dict | None:
    from openkb.source_pages import PageRangeError, validate_source_ranges

    validate_source_ranges(page_range, char_range, block_range)
    root = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(root, scope)
    with kb_read_lock(root / ".openkb"):
        sources = list_sources(root)
        source = next((source for source in sources if source.source_id == identifier), None)
        if source is None:
            return None
        units = list_source_units(root, source.source_id)
        if unit_id is not None:
            unit = next((item for item in units if item.unit_id == unit_id), None)
            if unit is None:
                raise ValueError("Processing unit does not belong to this source")
        else:
            eligible = tuple(
                unit
                for unit in units
                if not (scope and scope.read_only) or _snapshot_source(root, scope, unit.unit_id)
            )
            unit = eligible[0] if eligible else None
        view_id = scope.view_id if scope else source_view_id(root, source)
        state = _publication(root, unit.unit_id, view_id) if unit else None
        if scope and state is None and source_view_id(root, source) != view_id:
            return None
        target_id = (
            read_record(
                root, "unit-revisions", state.target_revision_id, UnitRevision
            ).source_revision_id
            if state
            else source.target_revision_id
        )
        if source_view_id(root, source) == view_id and not (scope and scope.read_only):
            target_id = source.target_revision_id
        target = read_source_revision(root, source_revision_id or target_id)
        if target.source_id != source.source_id:
            raise ValueError("Source revision belongs to another document")
        actual = _manifest(root, state) if state else None
        # The current source body belongs to its last successful input, even if a
        # newer input failed. Explicit history never falls back to different bytes.
        if scope and scope.read_only:
            actual = _snapshot_source(root, scope, unit.unit_id) if unit else None
            if actual is None or actual[1].unit_revision_id is None:
                return None
            used = read_record(root, "unit-revisions", actual[1].unit_revision_id, UnitRevision)
            if source_revision_id and source_revision_id != used.source_revision_id:
                return None
            target = read_source_revision(root, used.source_revision_id)
            state = UnitPublication.model_validate_json(
                (actual[0] / "publication.json").read_text()
            )
            target_id = target.source_revision_id
        elif actual and state and state.successful_revision_id:
            used = read_record(root, "unit-revisions", state.successful_revision_id, UnitRevision)
            if used.unit_id != state.unit_id:
                raise ValueError("Successful revision belongs to another unit")
            if source_revision_id is None or source_revision_id == used.source_revision_id:
                target = read_source_revision(root, used.source_revision_id)
                if target.source_id != source.source_id:
                    raise ValueError("Successful source revision belongs to another document")
            else:
                actual = _historical_manifest(root, state, source_revision_id)
        if (
            state
            and state.status == "empty"
            and not (scope and scope.read_only)
            and (source_revision_id is None or source_revision_id == target_id)
        ):
            actual = None
            target = read_source_revision(root, target_id)
        if scope and actual is None:
            assigned = any(
                annotation.source_id == source.source_id
                and annotation.source_revision_id == target.source_revision_id
                and annotation.view_id == scope.view_id
                for path in (root / ".openkb/catalog/annotations").glob("*.json")
                for annotation in [read_record(root, "annotations", path.stem, VersionAnnotation)]
            )
            if not assigned and not (scope.view_id == "legacy" and source.annotation_id is None):
                return None
        content = (
            state.message
            if state and state.message
            else "Frozen original retained; no published body for this revision."
        )
        pages = None
        selection = {}
        base_path = root / target.original
        contained_paths(root, [base_path])
        artifact_dir = root / ".openkb/artifacts" / target.digest
        if base_path.resolve().parent != artifact_dir or artifact_dir.resolve() != artifact_dir:
            raise ValueError("Frozen original moved outside its artifact directory")
        if actual:
            directory, manifest = actual
            if manifest.normalized_source is None:
                raise ValueError("Published source revision is missing its body reference")
            source_path = (
                manifest.source_map.path if manifest.source_map else manifest.normalized_source
            )
            base_path = directory / "wiki" / source_path
            contained_paths(root, [base_path])
            if not base_path.resolve().is_relative_to(directory / "wiki"):
                raise ValueError("Source body is outside its knowledge snapshot")
        if actual or target.original_kind == "legacy_snapshot":
            if actual and actual[1].source_map:
                from openkb.source_map import read_source_map

                selection = read_source_map(
                    actual[0] / "wiki",
                    actual[1].source_map,
                    unit.doc_name if unit else source.doc_name,
                    page_range,
                    chars=char_range,
                    blocks=block_range,
                )
            elif base_path.suffix == ".json":
                from openkb.source_pages import read_page_selection

                if char_range is not None or block_range is not None:
                    raise PageRangeError(
                        "This retained source has no frozen character or block map"
                    )
                page_list = json.loads(base_path.read_text("utf-8"))
                selection = read_page_selection(page_list, page_range)
            else:
                if page_range is not None or char_range is not None or block_range is not None:
                    raise PageRangeError("This retained source has no physical-page map")
                content = base_path.read_text("utf-8")
            if selection:
                content, pages = selection["content"], selection["pages"]
        elif state:
            from openkb.normalization import read_retained_source

            retained = read_retained_source(
                root,
                state.target_revision_id,
                target.source_revision_id,
                unit.doc_name if unit else source.doc_name,
                pages=page_range,
                chars=char_range,
                blocks=block_range,
            )
            if retained:
                base_path, selection = retained
                content, pages = selection["content"], selection["pages"]
        if not selection and any(
            value is not None for value in (page_range, char_range, block_range)
        ):
            raise PageRangeError("No retained source map is available for this range")
        annotation_id = source.annotation_id
        if actual and actual[1].unit_revision_id:
            body_revision = read_record(
                root, "unit-revisions", actual[1].unit_revision_id, UnitRevision
            )
            annotation_id = body_revision.annotation_id
        annotation = (
            read_record(root, "annotations", annotation_id, VersionAnnotation)
            if annotation_id
            else None
        )
        metadata = (
            annotation.metadata.model_dump(mode="json")
            if annotation and annotation.source_revision_id == target.source_revision_id
            else {}
        )
        from openkb.office.readback import read_office_artifacts
        from openkb.source_changes import source_validity
        from openkb.workbooks.catalog import inventory_failure

        inventory_error = (
            inventory_failure(root, source_revision_id or target_id)
            if not (scope and scope.read_only)
            else None
        )

        office = read_office_artifacts(
            root,
            actual[1].unit_revision_id if actual else state.target_revision_id if state else None,
            target.source_revision_id,
        )

        decoding = target.text_decoding
        input_diagnostics = (
            {
                "encoding": decoding.encoding.model_dump(mode="json")
                if decoding.encoding
                else None,
                "diagnostics": [
                    *decoding.diagnostics,
                    *([content] if not actual and content else []),
                ],
            }
            if decoding
            else {}
        )
        return {
            **input_diagnostics,
            **office,
            "hash": source.source_id,
            "validity": source_validity(
                root,
                source,
                view_id,
                target.source_revision_id,
                historical=bool(source_revision_id or (scope and scope.read_only)),
                unit_revision_id=actual[1].unit_revision_id
                if actual
                else state.target_revision_id
                if state and state.status in {"empty", "retired"}
                else None,
            ),
            "source_id": source.source_id,
            "view_id": view_id,
            "version_metadata": metadata,
            "original_kind": target.original_kind,
            "source_revision_id": target.source_revision_id,
            "unit_revision_id": actual[1].unit_revision_id if actual else None,
            "processing_fingerprint": body_revision.processing_fingerprint
            if actual and actual[1].unit_revision_id
            else None,
            **processing_details(root, state, actual[1] if actual else None),
            "target_source_revision_id": target_id,
            "knowledge_revision_id": actual[1].knowledge_revision_id if actual else None,
            "name": source.name,
            "doc_name": unit.doc_name if unit else source.doc_name,
            "unit_id": unit.unit_id if unit else None,
            "available_units": unit_inventory(root, units, view_id, scope=scope),
            "type": target.source_format,
            "format": "markdown",
            "content": content,
            "pages": pages,
            "original_path": target.original,
            "base_path": (
                base_path.parent.parent
                if base_path.suffix == ".json"
                and selection.get("unit_kind") not in {"text", "block"}
                else base_path.parent
            )
            .relative_to(root)
            .as_posix(),
            "status": "failed" if inventory_error else state.status if state else "admitted",
            "message": inventory_error or (state.message if state else None),
            "error_type": state.error_type if state else None,
            **selection,
        }
