"""Original previews and catalogs drawn entirely from a fixed publication closure."""

from ultrakb.ingest_records import ImportUnit, UnitPublication, UnitRevision
from ultrakb.knowledge_scope import KnowledgeScope, reading_scope
from ultrakb.source_catalog import read_record
from ultrakb.source_changes import source_validity
from ultrakb.source_records import Source, SourceRevision


def _decoding(revision):
    decoding = revision.text_decoding
    if decoding is None:
        return {}
    return {
        "encoding": decoding.encoding.model_dump(mode="json") if decoding.encoding else None,
        "diagnostics": list(decoding.diagnostics),
    }


def effective_input(root, view_id, identity):
    from ultrakb.knowledge_evidence import validated_input

    _, frozen = validated_input(root, identity, view_id)
    source = read_record(root, "sources", frozen.source_id, Source)
    return (
        not (source.removed or source.contribution_empty or identity in source.excluded_inputs)
        and source.target_revision_id == frozen.source_revision_id
    )


def retained_reprocessing_input(root, view_id, identity):
    from ultrakb.knowledge_evidence import validated_input
    from ultrakb.retained_reprocessing import _same_original
    from ultrakb.state import HashRegistry

    _, frozen = validated_input(root, identity, view_id)
    source = read_record(root, "sources", frozen.source_id, Source)
    if source.removed or source.contribution_empty or identity in source.excluded_inputs:
        return None
    if source.target_revision_id == frozen.source_revision_id:
        return None
    target = read_record(root, "source-revisions", source.target_revision_id, SourceRevision)
    revision, seen = target, set()
    while revision.source_revision_id != frozen.source_revision_id:
        if (
            revision.source_revision_id in seen
            or not _same_original(revision, frozen)
            or not revision.reprocessed_from
            or not revision.reprocessing_request
        ):
            return None
        seen.add(revision.source_revision_id)
        revision = read_record(root, "source-revisions", revision.reprocessed_from, SourceRevision)
    original = (root / frozen.original).resolve()
    if not original.is_relative_to(root) or not original.is_file():
        return None
    if HashRegistry.hash_file(original) != frozen.digest:
        return None
    return {
        "processing_status": "retained_before_reprocessing",
        "target_source_revision_id": target.source_revision_id,
        "interpretation": (
            "Retained successful processing of the same original; refresh is pending."
        ),
    }


def sources(scope: KnowledgeScope) -> tuple[Source, ...]:
    public = scope.publication
    if public is None:
        raise ValueError("A public source reader requires a fixed publication")
    root = public.metadata_dir
    return tuple(
        read_record(root, "sources", path.stem, Source)
        for path in sorted((root / ".ultrakb/catalog/sources").glob("*.json"))
    )


def _units(scope, source_id):
    root = scope.publication.metadata_dir
    return tuple(
        unit
        for path in sorted((root / ".ultrakb/catalog/units").glob("*.json"))
        if (unit := read_record(root, "units", path.stem, ImportUnit)).source_id == source_id
    )


def _state(scope, unit):
    from ultrakb.unit_publication import publication_id

    try:
        state = read_record(
            scope.publication.metadata_dir,
            "publications",
            publication_id(unit.unit_id, scope.view_id),
            UnitPublication,
        )
    except FileNotFoundError:
        return None
    if state.unit_id != unit.unit_id or state.view_id != scope.view_id:
        raise ValueError("Publication belongs to another unit or view")
    return state


def _actual(scope, unit, source_revision_id=None):
    from ultrakb.application.sources import _snapshot_source
    from ultrakb.file_state import contained_paths

    if source_revision_id is not None:
        from ultrakb.ingest_records import KnowledgeRevision

        public = scope.publication
        catalog = public.metadata_dir
        manifest = public.manifest
        referenced = set(manifest.input_revisions) | {
            identity for values in manifest.page_dependencies.values() for identity in values
        }
        matching = {
            identity
            for identity in referenced
            if (revision := read_record(catalog, "unit-revisions", identity, UnitRevision)).unit_id
            == unit.unit_id
            and revision.source_revision_id == source_revision_id
        }
        directory, seen = public.revision_dir, set()
        while matching and manifest.knowledge_revision_id not in seen:
            seen.add(manifest.knowledge_revision_id)
            if manifest.unit_revision_id in matching:
                return directory, manifest
            if manifest.base_revision_id is None:
                return None
            directory = (
                scope.kb_dir
                / ".ultrakb/knowledge"
                / scope.view_id
                / "revisions"
                / manifest.base_revision_id
            )
            contained_paths(scope.kb_dir, [directory])
            manifest = KnowledgeRevision.model_validate_json(
                (directory / "manifest.json").read_text("utf-8")
            )
            if (
                manifest.view_id != scope.view_id
                or manifest.knowledge_revision_id != directory.name
            ):
                raise ValueError("Knowledge snapshot ancestry belongs to another view")
        return None
    return _snapshot_source(scope.kb_dir, scope, unit.unit_id)


def _unit_info(scope, unit):
    from ultrakb.application.sources import processing_details
    from ultrakb.source_metrics import unit_metrics

    state, actual = _state(scope, unit), _actual(scope, unit)
    root = scope.publication.metadata_dir
    used = (
        read_record(root, "unit-revisions", actual[1].unit_revision_id, UnitRevision)
        if actual and actual[1].unit_revision_id
        else None
    )
    target = read_record(root, "unit-revisions", unit.target_revision_id, UnitRevision)
    return {
        **(state.model_dump(mode="json") if state else {"status": "pending"}),
        **processing_details(root, state, actual[1] if actual else None),
        **unit_metrics(root, state, actual, unit.doc_name),
        "target_source_revision_id": target.source_revision_id,
        "successful_source_revision_id": used.source_revision_id if used else None,
        "unit_id": unit.unit_id,
        "key": unit.key,
        "name": unit.name or unit.doc_name,
        "doc_name": unit.doc_name,
    }


def source_inventory(scope: KnowledgeScope) -> list[dict]:
    from ultrakb.application.sources import processing_details, source_status
    from ultrakb.source_metrics import unit_metrics

    public = scope.publication
    if public is None:
        raise ValueError("A public source reader requires a fixed publication")
    root = public.metadata_dir
    documents = []
    with reading_scope(scope):
        for source in sources(scope):
            if source.removed:
                continue
            units = _units(scope, source.source_id)
            states = [state for unit in units if (state := _state(scope, unit))]
            actual = _actual(scope, units[0]) if units else None
            used = (
                read_record(root, "unit-revisions", actual[1].unit_revision_id, UnitRevision)
                if actual and actual[1].unit_revision_id
                else None
            )
            revision = read_record(
                root,
                "source-revisions",
                used.source_revision_id if used else source.target_revision_id,
                SourceRevision,
            )
            manifest = actual[1] if actual else None
            documents.append(
                {
                    **(
                        unit_metrics(root, states[0] if states else None, actual, units[0].doc_name)
                        if len(units) == 1
                        else {}
                    ),
                    "hash": source.source_id,
                    "source_id": source.source_id,
                    "source_generation": source.target_generation,
                    "legacy_hash": source.legacy_hash,
                    "source_revision_id": revision.source_revision_id,
                    "validity": source_validity(
                        source,
                        revision.source_revision_id,
                        unit_revision_id=used.unit_revision_id if used else None,
                    ),
                    "view_id": scope.view_id,
                    "publication_id": scope.publication_id,
                    "name": source.name,
                    "type": revision.source_format,
                    "doc_name": source.doc_name,
                    "unit_count": len(units),
                    "display_type": "pageindex"
                    if manifest and manifest.execution_mode == "segmented"
                    else "short",
                    "status": source_status(states, expected_count=len(units)),
                    "message": states[0].message if states else None,
                    "units": [_unit_info(scope, unit) for unit in units],
                    "pages": manifest.source_map.unit_count
                    if manifest and manifest.source_map and manifest.source_map.unit_kind == "page"
                    else None,
                    "original_path": (root / revision.original)
                    .relative_to(scope.kb_dir)
                    .as_posix(),
                    **processing_details(root, states[0] if states else None, manifest),
                }
            )
    return documents


def read_source(
    scope: KnowledgeScope,
    identifier: str,
    *,
    source_revision_id=None,
    unit_id=None,
    pages=None,
    chars=None,
    blocks=None,
) -> dict | None:
    from ultrakb.application.sources import processing_details
    from ultrakb.conversion_artifacts import read_conversion_artifacts
    from ultrakb.source_map import read_source_map
    from ultrakb.source_pages import PageRangeError

    public = scope.publication
    if public is None:
        raise ValueError("A public source reader requires a fixed publication")
    root = public.metadata_dir
    with reading_scope(scope):
        source = next((source for source in sources(scope) if source.source_id == identifier), None)
        if source is None:
            return None
        units = _units(scope, source.source_id)
        if unit_id is not None and not any(unit.unit_id == unit_id for unit in units):
            raise ValueError("Processing unit does not belong to this source")
        eligible = [unit for unit in units if unit_id is None or unit.unit_id == unit_id]
        unit = next((unit for unit in eligible if _actual(scope, unit, source_revision_id)), None)
        if unit is None:
            return (
                _retained_source(
                    scope,
                    source,
                    eligible[0],
                    source_revision_id=source_revision_id,
                    pages=pages,
                    chars=chars,
                    blocks=blocks,
                )
                if eligible
                else None
            )
        directory, manifest = _actual(scope, unit, source_revision_id)
        used = read_record(root, "unit-revisions", manifest.unit_revision_id, UnitRevision)
        if source_revision_id is not None and source_revision_id != used.source_revision_id:
            return None
        revision = read_record(root, "source-revisions", used.source_revision_id, SourceRevision)
        if revision.source_id != source.source_id:
            raise ValueError("Source revision belongs to another document")
        if manifest.source_map:
            selection = read_source_map(
                directory / "wiki",
                manifest.source_map,
                unit.doc_name,
                pages,
                chars=chars,
                blocks=blocks,
            )
            content = selection["content"]
            body_path = directory / "wiki" / manifest.source_map.path
        else:
            if any(value is not None for value in (pages, chars, blocks)):
                raise PageRangeError("This retained source has no frozen reading map")
            if not manifest.normalized_source:
                return None
            body_path = directory / "wiki" / manifest.normalized_source
            content, selection = body_path.read_text("utf-8"), {}
        state = _state(scope, unit)
        conversion = read_conversion_artifacts(root, used.unit_revision_id, used.source_revision_id)
        if conversion.get("internal_pdf_path"):
            conversion["internal_pdf_path"] = (
                (root / conversion["internal_pdf_path"]).relative_to(scope.kb_dir).as_posix()
            )
        return {
            **_decoding(revision),
            **conversion,
            **processing_details(root, state, manifest),
            "hash": source.source_id,
            "source_id": source.source_id,
            "publication_id": scope.publication_id,
            "view_id": scope.view_id,
            "validity": source_validity(
                source, revision.source_revision_id, unit_revision_id=used.unit_revision_id
            ),
            "name": source.name,
            "doc_name": unit.doc_name,
            "unit_id": unit.unit_id,
            "source_revision_id": revision.source_revision_id,
            "target_source_revision_id": source.target_revision_id,
            "unit_revision_id": used.unit_revision_id,
            "processing_fingerprint": used.processing_fingerprint,
            "knowledge_revision_id": public.knowledge_revision_id,
            "type": revision.source_format,
            "format": "markdown",
            "content": content,
            "pages": selection.get("pages"),
            "available_units": [_unit_info(scope, item) for item in units],
            "original_path": (root / revision.original).relative_to(scope.kb_dir).as_posix(),
            "original_kind": revision.original_kind,
            "base_path": (
                directory / "wiki"
                if body_path.suffix == ".json"
                and selection.get("unit_kind") not in {"text", "block"}
                else body_path.parent
            )
            .relative_to(scope.kb_dir)
            .as_posix(),
            "status": state.status if state else "completed",
            "message": state.message if state else None,
            **selection,
        }


def _retained_source(scope, source, unit, *, source_revision_id, pages, chars, blocks):
    from ultrakb.application.sources import processing_details
    from ultrakb.conversion_artifacts import read_conversion_artifacts
    from ultrakb.normalization import read_retained_source
    from ultrakb.source_pages import PageRangeError

    root = scope.publication.metadata_dir
    state = _state(scope, unit)
    target_id = source_revision_id or source.target_revision_id
    target = read_record(root, "source-revisions", target_id, SourceRevision)
    if target.source_id != source.source_id:
        raise ValueError("Source revision belongs to another document")
    input_id = state.target_revision_id if state else unit.target_revision_id
    retained = read_retained_source(
        root, input_id, target_id, unit.doc_name, pages=pages, chars=chars, blocks=blocks
    )
    if retained is None and chars is None and blocks is None:
        from ultrakb.application.retained_documents import read_retained_images

        retained = read_retained_images(root, target_id, unit_revision_id=input_id, pages=pages)
    if retained is not None:
        body_path, selection = retained
        content = selection["content"]
    else:
        if any(value is not None for value in (pages, chars, blocks)):
            raise PageRangeError("No retained source map is available for this range")
        body_path, selection = root / target.original, {}
        content = (
            state.message
            if state and state.message
            else ("Frozen original retained; no published body for this revision.")
        )
    conversion = read_conversion_artifacts(root, input_id, target_id)
    if conversion.get("internal_pdf_path"):
        conversion["internal_pdf_path"] = (
            (root / conversion["internal_pdf_path"]).relative_to(scope.kb_dir).as_posix()
        )
    return {
        **_decoding(target),
        **conversion,
        **processing_details(root, state, None),
        "hash": source.source_id,
        "source_id": source.source_id,
        "publication_id": scope.publication_id,
        "view_id": scope.view_id,
        "validity": source_validity(source, target_id, unit_revision_id=input_id),
        "name": source.name,
        "doc_name": unit.doc_name,
        "unit_id": unit.unit_id,
        "source_revision_id": target_id,
        "target_source_revision_id": source.target_revision_id,
        "unit_revision_id": None,
        "knowledge_revision_id": None,
        "type": target.source_format,
        "format": "markdown",
        "content": content,
        "pages": selection.get("pages"),
        "available_units": [_unit_info(scope, item) for item in _units(scope, source.source_id)],
        "original_path": (root / target.original).relative_to(scope.kb_dir).as_posix(),
        "original_kind": target.original_kind,
        "base_path": (
            body_path.parent.parent
            if body_path.suffix == ".json" and selection.get("unit_kind") not in {"text", "block"}
            else body_path.parent
        )
        .relative_to(scope.kb_dir)
        .as_posix(),
        "status": state.status if state else "admitted",
        "message": state.message if state else None,
        **selection,
    }
