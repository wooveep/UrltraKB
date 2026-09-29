"""Conservative family matching; absent applicability never means every version."""

from dataclasses import dataclass, field
from pathlib import Path

from openkb.application.views import source_annotation
from openkb.knowledge_scope import KnowledgeScope
from openkb.source_catalog import Admission, list_sources, read_record
from openkb.view_records import KnowledgeView, SourceMetadata, VersionAnnotation, VersionCandidate


@dataclass(frozen=True)
class VersionAssessment:
    metadata: SourceMetadata
    related: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    reason: str = ""
    candidates: tuple[VersionCandidate, ...] = ()
    evidence: dict[str, str] = field(default_factory=dict)
    conflicts: tuple[str, ...] = ()


def assess_version(
    kb_dir: Path,
    admission: Admission,
    metadata: SourceMetadata | None,
    *,
    scope: KnowledgeScope | None = None,
    candidates: tuple[VersionCandidate, ...] | None = None,
) -> VersionAssessment:
    from openkb.version_evidence import pdf_title_candidates

    previous = source_annotation(kb_dir, admission)
    same_input = previous and previous.source_revision_id == admission.revision.source_revision_id
    values = (previous.metadata if same_input and previous else SourceMetadata()).model_dump()
    if previous and not same_input:
        values.update(product=previous.metadata.product, family=previous.metadata.family)
    if candidates is None:
        try:
            candidates = (
                previous.candidates
                if previous and same_input
                else pdf_title_candidates(kb_dir / admission.revision.original)
                if admission.revision.source_format == "pdf"
                else ()
            )
        except (OSError, ValueError, RuntimeError):
            # Conversion owns malformed-input diagnostics and its durable failure.
            candidates = ()
    evidence = dict(previous.evidence) if previous and same_input else {}
    conflicts = set()
    for name in ("product", "applicable_versions", "family", "document_revision"):
        found = [item for item in candidates if item.field == name]
        choices = {item.values for item in found}
        if len(choices) == 1:
            value = next(iter(choices))
            values[name] = value if name == "applicable_versions" else value[0]
            evidence[name] = f"{found[0].location}: {found[0].excerpt}"
        elif len(choices) > 1:
            values[name] = () if name == "applicable_versions" else None
            conflicts.add(name)
    if previous and same_input:
        for name, origin in previous.evidence.items():
            if origin == "user":
                values[name] = getattr(previous.metadata, name)
                evidence[name] = "user"
                conflicts.discard(name)
    if metadata is not None:
        values.update(metadata.model_dump(exclude_unset=True))
        for name in metadata.model_fields_set - {"schema_version"}:
            evidence[name] = "user"
            conflicts.discard(name)
    if scope and scope.view_id != "legacy":
        chosen = read_record(kb_dir, "views", scope.view_id, KnowledgeView)
        values["product"] = values["product"] or chosen.product
        values["applicable_versions"] = values["applicable_versions"] or chosen.applicable_versions
        for name in ("product", "applicable_versions"):
            evidence[name] = "user"
            conflicts.discard(name)
    selected = SourceMetadata.model_validate(values)
    possible_products = {selected.product}
    possible_families = {selected.family}
    for candidate in candidates:
        if candidate.field == "product" and not selected.product:
            possible_products.update(candidate.values)
        if candidate.field == "family" and not selected.family:
            possible_families.update(candidate.values)
    related = []
    for source in list_sources(kb_dir):
        if source.removed or source.annotation_id is None:
            continue
        annotation = read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation)
        if annotation.source_id != source.source_id:
            raise ValueError("Version annotation belongs to another source")
        if annotation.source_revision_id == admission.revision.source_revision_id:
            continue
        if (
            source.source_id == admission.source.source_id
            and annotation.metadata.product in possible_products
            or annotation.metadata.family is not None
            and annotation.metadata.family in possible_families
            and annotation.metadata.product in possible_products
        ):
            related.append(annotation.annotation_id)
    missing = (
        tuple(
            sorted(
                {
                    field
                    for field in ("product", "applicable_versions")
                    if not getattr(selected, field)
                }
                | conflicts
            )
        )
        if related
        else ()
    )
    return VersionAssessment(
        selected,
        tuple(related),
        missing,
        "A related manual already exists; confirm applicability before compiling this input."
        if missing
        else "",
        candidates,
        evidence,
        tuple(sorted(conflicts)),
    )
