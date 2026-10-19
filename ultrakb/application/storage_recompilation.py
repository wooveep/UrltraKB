"""Retained-unit selection and confirmed batch ancestry for the main store."""

import hashlib
import json
from dataclasses import replace

from ultrakb.storage.catalog_store import CatalogStore

from .storage_inventory import catalog_sources


def target_fingerprint(scope, *, sources=None):
    catalog = CatalogStore(scope.kb_dir, scope.knowledge.identity)
    if sources is None:
        sources = [
            (source, catalog.source_units(source.source_id)) for source in catalog_sources(catalog)
        ]
    values = [
        (
            source.source_id,
            source.revision_id,
            source.generation,
            [(unit.unit_id, unit.unit_revision_id) for unit in units],
        )
        for source, units in sources
    ]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def confirmation_version(scope, *, targets=None, publication_id=None):
    identity = scope.knowledge.identity
    # Identity and generation are part of authorization, even after restoring
    # exactly the same publication and source revisions into a new directory.
    payload = (identity.kb_id, identity.generation, targets or target_fingerprint(scope))
    digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    return f"recompile-v1:{publication_id or scope.publication_id}:{digest}"


def select_retained(scope, identifier, *, all_docs, confirmation, unit_id):
    from .recompilation import RecompileSelection, RecompileTarget

    catalog = CatalogStore(scope.kb_dir, scope.knowledge.identity)
    sources = [
        (source, catalog.source_units(source.source_id)) for source in catalog_sources(catalog)
    ]
    # The token must describe these exact displayed targets. A later catalog
    # read may invalidate them, but cannot authorize an unseen replacement.
    fingerprint = target_fingerprint(scope, sources=sources)
    if not all_docs:
        exact = [
            item
            for item in sources
            if identifier in {item[0].source_id, item[0].label, item[0].source_key}
        ]
        unit_matches = [
            (source, tuple(unit for unit in units if unit.doc_name == identifier))
            for source, units in sources
        ]
        unit_matches = [item for item in unit_matches if item[1]]
        sources = (
            exact
            or unit_matches
            or [
                (source, units)
                for source, units in sources
                if identifier
                and identifier.casefold()
                in (source.label + " " + " ".join(unit.doc_name for unit in units)).casefold()
            ]
        )
    targets = tuple(
        RecompileTarget(
            source.source_id,
            source.label if len(units) == 1 else f"{source.label} · {unit.doc_name}",
            unit.doc_name,
            processing.decision.length_class,
            processing.decision.execution_mode,
            unit.unit_id,
        )
        for source, units in sources
        for unit in units
        if (unit_id is None or unit.unit_id == unit_id)
        and (processing := catalog.processing(unit.processing_id)).content_state == "content"
    )
    if not targets:
        return RecompileSelection("empty" if all_docs else "not_found")
    if not all_docs and len({target.file_hash for target in targets}) > 1:
        return RecompileSelection("multiple", targets)
    return RecompileSelection(
        "ready", targets, confirmation_version(scope, targets=fingerprint) if confirmation else None
    )


def advance_recompilation(scope, result):
    """Bind only this unit's durable publication; a concurrent head is never adopted."""
    if scope is None or scope.knowledge is None or result.status != "compiled":
        return scope
    publication_id = result.publication_id
    if publication_id is None:
        return scope
    from .views import storage_scope

    return storage_scope(
        scope.kb_dir, scope.knowledge.identity, publication_id=publication_id, view_id=scope.view_id
    )


def confirmed_result(scope, result, targets, *, confirmed):
    if result.status != "compiled":
        return result
    publication_id = result.publication_id or result.version
    return replace(
        result,
        publication_id=publication_id,
        version=confirmation_version(scope, targets=targets, publication_id=publication_id)
        if confirmed
        else result.version,
    )


def refresh_compilation_schema(scope, version=None):
    """Publish this explicit schema update and return only its own resulting baseline."""
    from ultrakb.schema import AGENTS_MD
    from ultrakb.storage.knowledge_store import PublicationConflict

    from .publications import publish_knowledge_changes
    from .views import storage_scope

    if version is not None and version != confirmation_version(scope):
        raise PublicationConflict("Retained inputs changed after confirmation")
    bound = scope.knowledge
    try:
        previous = bound.read("AGENTS.md")
    except FileNotFoundError:
        return scope, version, False
    if previous == AGENTS_MD.encode():
        return scope, version, False
    outcome = publish_knowledge_changes(
        bound, {"AGENTS.md": AGENTS_MD.encode(), "AGENTS.md.bak": previous}
    )
    if outcome.status != "published":
        raise PublicationConflict("Knowledge changed before schema refresh")
    publication_id = outcome.publication.publication_id
    selected = storage_scope(
        scope.kb_dir, bound.identity, publication_id=publication_id, view_id=scope.view_id
    )
    # Keep the reviewed target digest even if another source changes during
    # publication or projection. The next compile will reject that change.
    advanced = (
        f"recompile-v1:{publication_id}:{version.split(':', 2)[2]}" if version is not None else None
    )
    return selected, advanced, True
