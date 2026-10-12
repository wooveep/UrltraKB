"""Plan source validity and dependent-page changes for a caller's single transaction."""

from pathlib import Path
from typing import Literal

from openkb.ingest_records import KnowledgeHead, KnowledgeRevision, RefreshReason, UnitRevision
from openkb.source_catalog import read_record, read_source, read_source_revision
from openkb.source_records import Source
from openkb.unit_publication import read_head


def effective_input(kb_dir: Path, view_id: str, identity: str) -> bool:
    from openkb.knowledge_evidence import validated_input

    _, frozen = validated_input(kb_dir, identity, view_id)
    source = read_source(kb_dir, frozen.source_id)
    if source.removed or identity in source.excluded_inputs:
        return False
    return not source.contribution_empty and source.target_revision_id == frozen.source_revision_id


def source_validity(
    source: Source,
    source_revision_id: str,
    *,
    historical: bool = False,
    unit_revision_id: str | None = None,
) -> str:
    if historical:
        return "historical"
    if source.removed:
        return "withdrawn"
    if unit_revision_id is not None and unit_revision_id in source.excluded_inputs:
        return source.excluded_inputs[unit_revision_id]
    if source.contribution_empty:
        return "empty"
    if source_revision_id != source.target_revision_id:
        return "needs_refresh"
    return "current"


def exclude_source_inputs(
    kb_dir: Path,
    source: Source,
    kind: Literal["empty", "withdrawn"],
    *,
    unit_ids: set[str] | None = None,
) -> Source:
    """An exclusion belongs to an exact input; later imports cannot resurrect it."""
    from openkb.unit_publication import list_source_units

    units = {unit.unit_id for unit in list_source_units(kb_dir, source.source_id)}
    if unit_ids is not None:
        units &= unit_ids
    excluded = dict(source.excluded_inputs)
    for path in (kb_dir / ".openkb/catalog/unit-revisions").glob("*.json"):
        revision = read_record(kb_dir, "unit-revisions", path.stem, UnitRevision)
        if revision.unit_id not in units:
            continue
        frozen = read_source_revision(kb_dir, revision.source_revision_id)
        if frozen.source_id != source.source_id:
            raise ValueError("Excluded input belongs to another source")
        excluded[revision.unit_revision_id] = kind
    return source.model_copy(update={"excluded_inputs": excluded})


def source_change_heads(
    kb_dir: Path,
    source: Source,
    kind: Literal["updated", "withdrawn", "empty"],
    *,
    unit_ids: set[str] | None = None,
) -> dict[Path, KnowledgeHead]:
    """No writes or model calls: caller commits these alongside the changed Source."""
    records = {}
    path = kb_dir / ".openkb/knowledge/legacy/head.json"
    head = read_head(kb_dir)
    if head.knowledge_revision_id:
        manifest = KnowledgeRevision.model_validate_json(
            (path.parent / "revisions" / head.knowledge_revision_id / "manifest.json").read_text()
        )
        if manifest.view_id != head.view_id:
            raise ValueError("Knowledge manifest belongs to another view")
        affected = {}
        for identity in set(head.inputs.values()) | {
            value for values in manifest.page_dependencies.values() for value in values
        }:
            unit = read_record(kb_dir, "unit-revisions", identity, UnitRevision)
            if unit_ids is not None and unit.unit_id not in unit_ids:
                continue
            frozen = read_source_revision(kb_dir, unit.source_revision_id)
            if frozen.source_id == source.source_id and not (
                kind == "updated" and identity in source.excluded_inputs
            ):
                affected[identity] = (frozen.source_revision_id, unit.unit_id)
        if affected:
            pending = dict(head.needs_refresh)
            for page, dependencies in manifest.page_dependencies.items():
                if not page.startswith(
                    (
                        "summaries/",
                        "concepts/",
                        "entities/",
                        "reports/",
                        "explorations/",
                        "index.md",
                    )
                ):
                    continue
                stale = set(dependencies) & affected.keys()
                if not stale:
                    continue
                retained = tuple(
                    r
                    for r in pending.get(page, ())
                    if r.source_id != source.source_id
                    or unit_ids is not None
                    and r.unit_id not in unit_ids
                )
                pending[page] = retained + tuple(
                    RefreshReason(
                        source_id=source.source_id,
                        source_revision_id=affected[identity][0],
                        unit_id=affected[identity][1],
                        target_revision_id=source.target_revision_id,
                        source_generation=source.target_generation,
                        kind=kind,
                    )
                    for identity in sorted(stale)
                )
            records[path] = head.model_copy(
                update={"generation": head.generation + 1, "needs_refresh": pending}
            )
    if (
        source.legacy_hash
        and source.legacy_hash in unpublished_legacy_inputs(kb_dir)
        and (kb_dir / ".openkb/catalog/sources" / f"{source.source_id}.json").exists()
    ):
        # Legacy pages have no trustworthy per-page dependencies. Preserve the
        # body and conservatively mark the shared wiki until its inputs are migrated.
        from openkb.unit_publication import wiki_versions

        path = kb_dir / ".openkb/knowledge/legacy/head.json"
        head = records.get(path, read_head(kb_dir, "legacy"))
        actual = read_source(kb_dir, source.source_id)
        pending = dict(head.needs_refresh)
        reason = RefreshReason(
            source_id=source.source_id,
            source_revision_id=actual.target_revision_id,
            target_revision_id=source.target_revision_id,
            source_generation=source.target_generation,
            kind=kind,
        )
        for page in wiki_versions(kb_dir, kb_dir / "wiki"):
            if page in {"AGENTS.md", "log.md"}:
                continue
            pending[page] = tuple(
                r for r in pending.get(page, ()) if r.source_id != source.source_id
            ) + (reason,)
        records[path] = head.model_copy(
            update={"generation": head.generation + 1, "needs_refresh": pending}
        )
    return records


def unpublished_legacy_inputs(kb_dir: Path) -> dict[str, str]:
    """Missing migration records are unknown contributions, never empty ones."""
    from openkb.source_catalog import list_sources
    from openkb.state import HashRegistry
    from openkb.unit_publication import list_source_units

    head = read_head(kb_dir, "legacy")
    sources = {s.legacy_hash: s for s in list_sources(kb_dir) if s.legacy_hash}
    missing = {}
    for identity, meta in HashRegistry(kb_dir / ".openkb/hashes.json").all_entries().items():
        source = sources.get(identity)
        if source is None or not any(
            unit.unit_id in head.inputs for unit in list_source_units(kb_dir, source.source_id)
        ):
            missing[identity] = meta.get("name") or identity
    return missing


def filter_compile_context(kb_dir: Path, wiki: Path, head: KnowledgeHead) -> None:
    """Exclude pending pages and inactive sources from the model's working copy."""
    from openkb.ingest_records import ImportUnit
    from openkb.locks import atomic_write_text
    from openkb.schema import INDEX_SEED

    for page in head.needs_refresh:
        (wiki / page).unlink(missing_ok=True)
    for unit_id, revision_id in head.inputs.items():
        if effective_input(kb_dir, head.view_id, revision_id):
            continue
        unit = read_record(kb_dir, "units", unit_id, ImportUnit)
        for suffix in ("md", "json", "content.json"):
            (wiki / "sources" / f"{unit.doc_name}.{suffix}").unlink(missing_ok=True)
    if head.needs_refresh:
        atomic_write_text(wiki / "index.md", INDEX_SEED)


def retain_pending_pages(
    kb_dir: Path, wiki: Path, head: KnowledgeHead, *, replacing_unit_id: str | None = None
) -> None:
    """Untouched stale results remain readable; only explicit refresh clears their reasons."""
    from openkb.knowledge_scope import live_scope
    from openkb.mutation import _copy_file_atomic

    live = live_scope(kb_dir, head.view_id).wiki_dir
    for page in head.needs_refresh:
        if not (wiki / page).exists() and (live / page).is_file():
            _copy_file_atomic(live / page, wiki / page)
            from openkb.figure_publication import preserve_page_figures

            preserve_page_figures(kb_dir, wiki, head.inputs, source_wiki=live, pages=[page])
    from openkb.retained_reprocessing import restore_retained_originals

    restore_retained_originals(kb_dir, wiki, head, replacing_unit_id)
