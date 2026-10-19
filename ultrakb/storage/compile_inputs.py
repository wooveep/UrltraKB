"""Exact retained source identities and private inputs for the file compiler."""

from dataclasses import dataclass

from .blobs import Blob, materialize_private
from .catalog_store import CatalogStore
from .coordination import storage_stage
from .knowledge_inputs import image_paths, source_paths


@dataclass
class CompileInputs:
    identities: dict
    originals: dict

    def identity(self, path):
        return dict(self.identities.get(path, {}))

    def page_originals(self, page):
        return self.originals.get(page)


def materialize_inputs(base, baseline, target):
    """Keep old page inputs distinct when a document's current target advances."""
    identities, paths = {}, {}
    current = {unit.unit_revision_id for unit in base.input_units()}
    basis = base.page_input_revisions()
    units = current | {target.unit_revision_id} | {u for values in basis.values() for u in values}
    native = {}
    with storage_stage(base.root, base.identity, read_only=True) as db:
        for revision in sorted(units):
            unit = CatalogStore._unit_revision(db, revision)
            processing = CatalogStore._processing(db, unit.processing_id)
            retained = revision != target.unit_revision_id and (
                revision not in current or unit.unit_id == target.unit_id
            )
            aliases = source_paths(unit, processing, retained=retained)
            files = {file.path: file for file in processing.files}
            native[revision] = aliases[-1][0]
            for alias, original in (*aliases, *image_paths(unit, processing, retained=retained)):
                file = files[original]
                previous = paths.get(alias)
                if previous is not None and previous != file.digest:
                    raise ValueError("Compile source aliases must identify one retained input")
                paths[alias] = file.digest
                materialize_private(
                    db,
                    Blob(file.digest, file.byte_count),
                    baseline.private,
                    alias,
                    replace=(baseline.directory / alias).exists(),
                )
                identities[alias] = {
                    "source_revision_id": unit.source_revision_id,
                    "processing_revision_id": unit.unit_revision_id,
                }
    return CompileInputs(
        identities,
        {page: sorted({native[unit] for unit in revisions}) for page, revisions in basis.items()},
    )


def prepare_compiled_revision(store, base, baseline, target, work):
    """Reuse unchanged immutable files and retain a unit's exact import claim."""
    import hashlib

    changes = {}
    for path in baseline.directory.rglob("*"):
        if path.is_file():
            relative = path.relative_to(baseline.directory).as_posix()
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != baseline.files.get(relative):
                changes[relative] = payload
    for path in baseline.files:
        if path.endswith(".md") and not (baseline.directory / path).exists():
            changes[path] = None
    active = tuple(u for u in base.input_units() if u.unit_id != target.unit_id)
    unit_revisions = tuple(u.unit_revision_id for u in active) + (target.unit_revision_id,)
    from .index_store import IndexStore

    indexes = tuple(
        index
        for index in base.index_revisions()
        if IndexStore(base.root, base.identity).revision(index).unit_revision_id in unit_revisions
    )
    basis = base.page_input_revisions()
    page_inputs = {
        path: tuple(dict.fromkeys((*basis.get(path, ()), target.unit_revision_id)))
        for path, value in changes.items()
        if value is not None and path.endswith(".md")
    }
    return store._prepare_revision(
        changes,
        expected=base.publication_id,
        view_id=base.publication.view_id,
        unit_revisions=unit_revisions,
        index_revisions=indexes,
        import_work=work,
        page_inputs={
            path: revisions
            for path, revisions in basis.items()
            if changes.get(path, b"") is not None
        }
        | page_inputs,
        base=base,
    )
