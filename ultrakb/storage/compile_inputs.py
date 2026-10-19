"""Exact retained source identities and private inputs for the file compiler."""

import hashlib
import io
import json
import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath

from ultrakb.agent.page_figures import split_gallery
from ultrakb.figure_assets import asset_path
from ultrakb.markdown_images import ImageReference, apply_image_edits, image_references
from ultrakb.source_qualification import source_images

from .blobs import Blob, materialize_private
from .catalog_store import CatalogStore
from .coordination import storage_stage
from .knowledge_inputs import image_paths, source_paths


@dataclass
class CompileInputs:
    identities: dict
    originals: dict
    assets: dict

    def identity(self, path):
        return dict(self.identities.get(path, {}))

    def page_originals(self, page):
        return self.originals.get(page)

    def figure_asset(self, source, path):
        return self.assets.get((source, path), path)


def _required_attachments(baseline):
    """Select references in captured Markdown, including historical gallery records."""
    selected = set()
    for path in baseline.files:
        if not path.endswith(".md"):
            continue
        text = (baseline.directory / path).read_text("utf-8")
        _, gallery = split_gallery(text)
        for entry in gallery:
            asset = entry["figure"]["asset_path"]
            if asset not in baseline.files:
                raise FileNotFoundError(asset)
            selected.add(asset)
        for reference in image_references(text):
            if reference.local_path is None:
                continue
            target = (baseline.directory / path).parent / reference.local_path
            target = target.resolve()
            if target.is_relative_to(baseline.directory):
                relative = target.relative_to(baseline.directory).as_posix()
                if relative in baseline.files:
                    selected.add(relative)
    if any(
        PurePosixPath(path).suffix.lower() in {".db", ".sqlite", ".sqlite3"} for path in selected
    ):
        raise ValueError("Index databases cannot be compile inputs")
    return selected


def _input_images(processing, native):
    text = native.read_text("utf-8")
    raw = text if processing.source_kind == "markdown" else json.loads(text)
    if isinstance(raw, list):
        paths = [entry["path"] for page in raw for entry in page.get("images", [])]
    else:
        references = (
            image_references(raw)
            if isinstance(raw, str)
            else [ImageReference(**ref) for ref in source_images(raw)]
        )
        paths = [ref.local_path for ref in references if ref.local_path is not None]
    return {f"sources/{path.removeprefix('sources/')}" for path in paths}


def _freeze_inline_assets(db, baseline):
    """Keep unchanged page images independent of a newer input's mutable filename."""
    for page in baseline.files:
        if not page.endswith(".md") or page.startswith("sources/"):
            continue
        text = (baseline.directory / page).read_text("utf-8")
        edits = []
        for reference in image_references(text):
            if reference.local_path is None:
                continue
            logical = posixpath.normpath(str(PurePosixPath(page).parent / reference.local_path))
            if logical not in baseline.files:
                continue
            data = (baseline.directory / logical).read_bytes()
            asset = asset_path(data)
            if asset == logical:
                continue
            if not (baseline.directory / asset).exists():
                materialize_private(
                    db, Blob(baseline.files[logical], len(data)), baseline.private, asset
                )
            edits.append(
                (
                    reference.start,
                    reference.end,
                    reference.relocated(
                        posixpath.relpath(asset, str(PurePosixPath(page).parent)),
                        preserve_suffix=True,
                    ),
                )
            )
        if edits:
            encoded = apply_image_edits(text, edits).encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            baseline.private.copy(
                io.BytesIO(encoded), page, digest=digest, byte_count=len(encoded), replace=True
            )
            baseline.normalized_pages[page] = digest


def materialize_inputs(base, baseline, target):
    """Keep old page inputs distinct when a document's current target advances."""
    identities, paths, assets = {}, {}, {}
    current = {unit.unit_revision_id for unit in base.input_units()}
    basis = base.page_input_revisions()
    units = current | {target.unit_revision_id} | {u for values in basis.values() for u in values}
    native = {}
    attachments = _required_attachments(baseline)
    with storage_stage(base.root, base.identity, read_only=True) as db:
        for path in sorted(attachments):
            if (baseline.directory / path).exists():
                continue
            digest = baseline.files[path]
            size = db.connection.execute(
                "SELECT byte_count FROM ukb_blobs WHERE digest = ? AND state = 'live'", (digest,)
            ).fetchone()
            if size is None:
                raise FileNotFoundError(path)
            materialize_private(db, Blob(digest, size[0]), baseline.private, path)
        _freeze_inline_assets(db, baseline)
        for revision in sorted(units):
            unit = CatalogStore._unit_revision(db, revision)
            processing = CatalogStore._processing(db, unit.processing_id)
            retained = revision != target.unit_revision_id and (
                revision not in current or unit.unit_id == target.unit_id
            )
            aliases = source_paths(unit, processing, retained=retained)
            files = {file.path: file for file in processing.files}
            native[revision] = aliases[-1][0]
            for alias, original in aliases:
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
            required = _input_images(processing, baseline.directory / native[revision])
            for alias, original in image_paths(unit, processing, retained=retained):
                canonical = f"sources/{original.removeprefix('sources/')}"
                if canonical not in required:
                    continue
                file = files[original]
                previous = paths.get(alias)
                if previous is not None and previous != file.digest:
                    raise ValueError("Compile image aliases must identify one retained input")
                paths[alias] = file.digest
                materialize_private(
                    db,
                    Blob(file.digest, file.byte_count),
                    baseline.private,
                    alias,
                    replace=(baseline.directory / alias).exists(),
                )
                for source, _ in aliases:
                    assets[source, original] = alias
                    assets[source, canonical] = alias
    return CompileInputs(
        identities,
        {page: sorted({native[unit] for unit in revisions}) for page, revisions in basis.items()},
        assets,
    )


def prepare_compiled_revision(store, base, baseline, target, work):
    """Reuse unchanged immutable files and retain a unit's exact import claim."""
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
        if value is not None
        and path.endswith(".md")
        and hashlib.sha256(value).hexdigest() != baseline.normalized_pages.get(path)
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
