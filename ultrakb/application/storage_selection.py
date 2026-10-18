"""Bind question evidence aliases to immutable managed processing resources."""

from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.knowledge_inputs import image_paths, source_paths, source_selector


def query_view(scope):
    from .query_views import QueryView

    bound = scope.knowledge
    with storage_stage(bound.root, bound.identity, read_only=True):
        files = bound.files()
        active = set(bound.unit_revisions())
        units = bound.input_units()
        revisions, native_inputs, selectors, unit_paths = {}, {}, {}, {}
        native_paths = []
        for unit in units:
            processing = CatalogStore(bound.root, bound.identity).processing(unit.processing_id)
            inventory = {file.path: file.digest for file in processing.files}
            retained = unit.unit_revision_id not in active
            paths = source_paths(unit, processing, retained=retained)
            native_paths.append(paths[-1][0])
            unit_paths[unit.unit_revision_id] = (paths[0][0], paths[-1][0])
            for alias, _ in paths:
                selectors[alias] = source_selector(unit, retained=retained)
            for alias, original in (*paths, *image_paths(unit, processing, retained=retained)):
                digest = inventory[original]
                if alias in files and files[alias] != digest:
                    raise ValueError("Source alias does not match its immutable processing input")
                files[alias] = digest
                native_inputs[alias] = (unit.unit_revision_id, original)
                revisions[alias] = unit.source_revision_id
        return QueryView(
            scope,
            bound.publication.revision_id,
            tuple(sorted({unit.source_revision_id for unit in units})),
            files,
            0,
            source_revisions_by_path=revisions,
            native_inputs=native_inputs,
            native_source_paths=tuple(native_paths),
            source_selectors=selectors,
            source_paths_by_unit=unit_paths,
        )


def page_source_paths(view, page, raw, *, native=True):
    """Resolve a page's source declaration against that page's retained input basis."""
    from ultrakb.lint import _normalize_target

    raw = raw.partition("#")[0]
    normalized = _normalize_target(raw)
    exact, fuzzy = [], []
    for unit in view.scope.knowledge.input_units(page=page):
        if unit.unit_revision_id not in view.source_paths_by_unit:
            continue
        body, original = view.source_paths_by_unit[unit.unit_revision_id]
        names = {
            unit.doc_name,
            f"{unit.doc_name}.md",
            f"{unit.doc_name}.json",
            f"{unit.doc_name}.content.json",
            f"sources/{unit.doc_name}",
            f"sources/{unit.doc_name}.md",
            f"sources/{unit.doc_name}.json",
            f"sources/{unit.doc_name}.content.json",
            f"summaries/{unit.doc_name}",
            f"summaries/{unit.doc_name}.md",
            body,
            original,
        }
        path = original if native else body
        if raw in names:
            exact.append(path)
        elif normalized in {_normalize_target(name) for name in names}:
            fuzzy.append(path)
    selected = exact or (fuzzy if len(fuzzy) == 1 else [])
    return tuple(dict.fromkeys(selected))
