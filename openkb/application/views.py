"""Resolve confirmed applicability without merging unknown or legacy evidence."""

import uuid
from pathlib import Path

from openkb.catalog_schema import CatalogSchema, catalog_schema_path
from openkb.knowledge_scope import KnowledgeScope, live_scope, resolve_scope
from openkb.locks import atomic_write_text, kb_ingest_lock, kb_read_lock
from openkb.mutation import mutation_scope
from openkb.schema import AGENTS_MD, INDEX_SEED
from openkb.source_catalog import Admission, read_record, record_path, write_record
from openkb.source_records import Record
from openkb.view_records import (
    DocumentFamily,
    KnowledgeView,
    Product,
    SourceMetadata,
    VersionAnnotation,
)


def list_views(kb_dir: Path) -> tuple[KnowledgeView, ...]:
    with kb_read_lock(kb_dir / ".openkb"):
        return (
            KnowledgeView(view_id="legacy"),
            *(
                read_record(kb_dir, "views", path.stem, KnowledgeView)
                for path in sorted((kb_dir / ".openkb/catalog/views").glob("*.json"))
            ),
        )


def map_legacy_sources(kb_dir: Path) -> dict:
    """Record retained legacy evidence without changing wiki pages or invoking a model."""
    from openkb.ingest_diagnostics import failure_reason
    from openkb.legacy_sources import admit_legacy_snapshot
    from openkb.state import HashRegistry

    root = kb_dir.resolve()
    with kb_ingest_lock(root / ".openkb"):
        sources, unavailable = [], {}
        for identity, metadata in HashRegistry(root / ".openkb/hashes.json").all_entries().items():
            try:
                if not isinstance(metadata, dict):
                    raise ValueError("Invalid legacy source metadata")
                sources.append(admit_legacy_snapshot(root, identity, metadata).source_id)
            except (OSError, ValueError) as exc:
                unavailable[identity] = failure_reason(exc)
        return {"source_ids": sources, "unavailable": unavailable}


def view_scope(
    kb_dir: Path, view_id: str, *, historical_revision: str | None = None
) -> KnowledgeScope:
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        if view_id != "legacy":
            read_record(root, "views", view_id, KnowledgeView)
        if historical_revision is None:
            return live_scope(root, view_id)
        from pydantic import TypeAdapter

        from openkb.ingest_records import KnowledgeRevision
        from openkb.source_records import RecordId

        TypeAdapter(RecordId).validate_python(historical_revision)
        directory = root / ".openkb/knowledge" / view_id / "revisions" / historical_revision
        scope = KnowledgeScope(root, directory / "wiki", view_id)
        manifest = KnowledgeRevision.model_validate_json(
            (directory / "manifest.json").read_text("utf-8")
        )
        if manifest.view_id != view_id or manifest.knowledge_revision_id != historical_revision:
            raise ValueError("Knowledge revision belongs to another view")
        return scope


def source_annotation(kb_dir: Path, admission: Admission) -> VersionAnnotation | None:
    identity = admission.source.annotation_id
    if identity is None:
        return None
    annotation = read_record(kb_dir, "annotations", identity, VersionAnnotation)
    if annotation.source_id != admission.source.source_id:
        raise ValueError("Version annotation belongs to another source")
    return annotation


def bind_source_view(
    kb_dir: Path,
    admission: Admission,
    metadata: SourceMetadata | None = None,
    *,
    scope: KnowledgeScope | None = None,
) -> tuple[Admission, KnowledgeScope]:
    """Persist metadata separately from bytes before model work. Caller owns its write lease."""
    root = kb_dir.resolve()
    with kb_ingest_lock(root / ".openkb"):
        previous = source_annotation(root, admission)
        confirmed: SourceMetadata = metadata or (
            previous.metadata if previous else SourceMetadata()
        )
        if scope is not None and scope.view_id != "legacy":
            chosen = read_record(root, "views", scope.view_id, KnowledgeView)
            supplied = confirmed
            confirmed = supplied.model_copy(
                update={
                    "product": supplied.product or chosen.product,
                    "applicable_versions": supplied.applicable_versions
                    or chosen.applicable_versions,
                }
            )
        records: dict[Path, Record] = {}
        product = None
        if confirmed.product:
            products = (
                read_record(root, "products", path.stem, Product)
                for path in (root / ".openkb/catalog/products").glob("*.json")
            )
            product = next((item for item in products if item.name == confirmed.product), None)
            if product is None:
                product = Product(product_id=uuid.uuid4().hex, name=confirmed.product)
                records[record_path(root, "products", product.product_id)] = product
        family = None
        if confirmed.family:
            families = (
                read_record(root, "families", path.stem, DocumentFamily)
                for path in (root / ".openkb/catalog/families").glob("*.json")
            )
            product_id = product.product_id if product else None
            family = next(
                (
                    item
                    for item in families
                    if item.product_id == product_id and item.purpose == confirmed.family
                ),
                None,
            )
            if family is None:
                family = DocumentFamily(
                    family_id=uuid.uuid4().hex, product_id=product_id, purpose=confirmed.family
                )
                records[record_path(root, "families", family.family_id)] = family
        versions = tuple(sorted(confirmed.applicable_versions))
        if scope is not None:
            scope = resolve_scope(root, scope, writable=True)
            selected = next(
                (item for item in list_views(root) if item.view_id == scope.view_id), None
            )
            if selected is None:
                raise ValueError("Unknown knowledge view")
            if selected.view_id == "legacy" and (confirmed.product or versions):
                raise ValueError("Legacy knowledge cannot be assigned a product version")
            if (
                selected.unknown_source_id
                and selected.unknown_source_id != admission.source.source_id
            ):
                raise ValueError("Unknown applicability cannot be shared by unrelated sources")
            if selected.view_id != "legacy" and (
                selected.product != confirmed.product or selected.applicable_versions != versions
            ):
                raise ValueError("Selected view does not match the source applicability")
        else:
            known = product is not None and bool(versions)
            selected = next(
                (
                    item
                    for item in list_views(root)
                    if item.view_id != "legacy"
                    and (
                        (
                            known
                            and item.unknown_source_id is None
                            and item.product_id == product.product_id
                            and item.applicable_versions == versions
                        )
                        if product is not None and known
                        else item.unknown_source_id == admission.source.source_id
                        and item.product == confirmed.product
                        and item.applicable_versions == versions
                    )
                ),
                None,
            )
            if selected is None:
                selected = KnowledgeView(
                    view_id=uuid.uuid4().hex,
                    product_id=product.product_id if product else None,
                    product=confirmed.product,
                    applicable_versions=versions,
                    unknown_source_id=None if known else admission.source.source_id,
                )
                records[record_path(root, "views", selected.view_id)] = selected
            scope = live_scope(root, selected.view_id)
        if (
            previous
            and previous.metadata == confirmed
            and previous.view_id == scope.view_id
            and previous.source_revision_id == admission.revision.source_revision_id
        ):
            return admission, scope
        annotation = VersionAnnotation(
            annotation_id=uuid.uuid4().hex,
            source_id=admission.source.source_id,
            source_revision_id=admission.revision.source_revision_id,
            previous_annotation_id=previous.annotation_id if previous else None,
            family_id=family.family_id if family else None,
            view_id=scope.view_id,
            metadata=confirmed,
            evidence={
                key: "user"
                for key, value in confirmed.model_dump().items()
                if value and key != "schema_version"
            },
        )
        source = admission.source.model_copy(
            update={
                "annotation_id": annotation.annotation_id,
                "family_id": annotation.family_id,
                "target_generation": admission.source.target_generation + 1,
            }
        )
        records.update(
            {
                record_path(root, "annotations", annotation.annotation_id): annotation,
                record_path(root, "sources", source.source_id): source,
                catalog_schema_path(root): CatalogSchema(),
            }
        )
        with mutation_scope(root, [*records, scope.wiki_dir], operation="bind-source-view"):
            for path, record in records.items():
                write_record(path, record)
            if not scope.wiki_dir.exists():
                for name in (
                    "sources",
                    "summaries",
                    "concepts",
                    "entities",
                    "explorations",
                    "reports",
                ):
                    (scope.wiki_dir / name).mkdir(parents=True, exist_ok=True)
                atomic_write_text(scope.wiki_dir / "AGENTS.md", AGENTS_MD)
                atomic_write_text(scope.wiki_dir / "index.md", INDEX_SEED)
                atomic_write_text(scope.wiki_dir / "log.md", "# Operations Log\n\n")
        return Admission(source, admission.revision, admission.discovery_intent), scope
