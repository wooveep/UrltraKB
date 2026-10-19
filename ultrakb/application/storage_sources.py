"""Fixed-publication source reads and explicit retained-input diagnostics."""

import json
from pathlib import PurePosixPath

from ultrakb.source_pages import PageRangeError, read_page_selection
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_inputs import image_paths, source_paths
from ultrakb.text_source import read_text_selection


def read_stored_source(
    scope, source_id, *, unit_id=None, source_revision_id=None, pages=None, chars=None, blocks=None
):
    """The ordinary public reader never falls back to live unpublished inputs."""
    base = scope.knowledge
    active = set(base.unit_revisions())
    published = sorted(
        base.input_units(),
        key=lambda unit: (unit.unit_revision_id not in active, unit.unit_revision_id),
    )
    candidates = [
        unit
        for unit in published
        if unit.source_id == source_id
        and (unit_id is None or unit.unit_id == unit_id)
        and (source_revision_id is None or unit.source_revision_id == source_revision_id)
    ]
    if not candidates:
        return None
    catalog = CatalogStore(scope.kb_dir, base.identity)
    return _payload(
        catalog,
        catalog.source(source_id),
        candidates[0],
        publication=base.publication,
        view_id=scope.view_id,
        retained=candidates[0].unit_revision_id not in active,
        pages=pages,
        chars=chars,
        blocks=blocks,
    )


def _retained_target(kb_dir, identity, source_id, unit_revision_id):
    catalog = CatalogStore(kb_dir, identity)
    target = catalog.unit_revision(unit_revision_id)
    if target.source_id != source_id:
        raise ValueError("Retained input belongs to another source")
    return catalog, target


def read_retained_input(
    kb_dir, source_id, unit_revision_id, *, identity, pages=None, chars=None, blocks=None
):
    """Explicit diagnostics select one immutable processing revision, not a Wiki head."""
    from ultrakb.source_pages import validate_source_ranges

    validate_source_ranges(pages, chars, blocks)
    catalog, target = _retained_target(kb_dir, identity, source_id, unit_revision_id)
    return _payload(
        catalog, catalog.source(source_id), target, pages=pages, chars=chars, blocks=blocks
    )


def read_retained_asset(kb_dir, source_id, unit_revision_id, path, *, identity):
    """Read only a file belonging to the explicitly selected retained input."""
    catalog, target = _retained_target(kb_dir, identity, source_id, unit_revision_id)
    return catalog.read_processed(target.processing_id, path)


def read_retained_original(kb_dir, source_id, unit_revision_id, *, identity):
    catalog, target = _retained_target(kb_dir, identity, source_id, unit_revision_id)
    return catalog.read_original(target.source_revision_id)


def _payload(
    catalog,
    source,
    target,
    *,
    publication=None,
    view_id=None,
    retained=False,
    pages=None,
    chars=None,
    blocks=None,
):
    processing = catalog.processing(target.processing_id)
    native = catalog.read_processed(target.processing_id, processing.source_path)
    files = {file.path: file for file in processing.files}
    images = image_paths(target, processing, retained=retained)
    selection = {}
    if processing.content_state in {"parse_failed", "objects_only"}:
        if any(value is not None for value in (pages, chars, blocks)):
            raise PageRangeError("This processing unit has no extracted native range")
        raw = json.loads(native)
        selection = {
            "content": "",
            "sheet": raw,
            "diagnostics": [raw.get("error") or processing.content_state],
        }
    elif processing.source_kind == "text":
        if pages is not None:
            raise PageRangeError("This source has no physical page coordinates")
        raw = json.loads(native)
        if blocks is not None:
            from ultrakb.block_package import read_block_selection

            selection = read_block_selection(raw, blocks)
        else:
            selection = read_text_selection(raw, chars)
    elif processing.source_kind == "pages":
        if chars is not None or blocks is not None:
            raise PageRangeError("This source requires physical page coordinates")
        raw = json.loads(native)
        if retained and publication is not None:
            from pageindex.backend.input_package import relocate_page

            aliases = {original: alias for alias, original in images}
            raw = [
                relocate_page(
                    page,
                    [aliases.get(image["path"], image["path"]) for image in page.get("images", [])],
                )
                for page in raw
            ]
        selection = read_page_selection(raw, pages)
    else:
        if any(value is not None for value in (pages, chars, blocks)):
            raise PageRangeError("This source has no retained native reading map")
        selection["content"] = native.decode("utf-8")
    metadata = {}
    if "retention.json" in files:
        from .storage_retained_inputs import RetainedContent

        receipt = RetainedContent.model_validate_json(
            catalog.read_processed(target.processing_id, "retention.json")
        )
        if receipt.source_revision_id != target.source_revision_id:
            raise ValueError("Retained content assessment belongs to another original")
        metadata["retention"] = receipt.model_dump(mode="json")
    if publication is not None:
        native_path = source_paths(target, processing, retained=retained)[-1][0]
        metadata.update(
            {
                "source_path": native_path,
                "base_path": "wiki"
                if processing.source_kind == "pages"
                else "wiki/" + str(PurePosixPath(native_path).parent),
            }
        )
    return {
        **selection,
        **metadata,
        "hash": source.source_id,
        "source_id": source.source_id,
        "unit_id": target.unit_id,
        "name": source.label,
        "doc_name": target.doc_name,
        "source_revision_id": target.source_revision_id,
        "target_source_revision_id": source.revision_id,
        "removed": source.removed,
        "contribution_empty": source.contribution_empty,
        "validity": "withdrawn"
        if source.removed
        else "empty"
        if source.contribution_empty
        else "historical"
        if retained
        else "needs_refresh"
        if source.revision_id != target.source_revision_id
        or catalog.unit(target.unit_id).unit_revision_id != target.unit_revision_id
        else "current",
        "unit_revision_id": target.unit_revision_id,
        "knowledge_revision_id": publication.revision_id if publication else None,
        "publication_id": publication.publication_id if publication else None,
        "view_id": view_id,
        "format": "markdown",
        "type": PurePosixPath(source.label).suffix.lstrip("."),
        "status": "retained_without_knowledge"
        if processing.content_state != "content"
        else "completed"
        if publication
        else "pending",
        "content_state": processing.content_state,
        "processing": processing.decision.model_dump(mode="json"),
        "processing_fingerprint": processing.fingerprint,
        "length_class": processing.decision.length_class,
        "execution_mode": processing.decision.execution_mode,
        "images": [
            {"path": alias if publication else original, "digest": files[original].digest}
            for alias, original in images
        ],
    }
