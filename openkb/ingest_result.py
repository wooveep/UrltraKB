"""Domain import outcomes, independent of one runtime attempt's receipt."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ImportUnitOutcome:
    unit_id: str
    status: str
    target_revision_id: str
    successful_revision_id: str | None = None
    successful_source_revision_id: str | None = None
    knowledge_revision_id: str | None = None
    view_id: str = "legacy"
    proposal_id: str | None = None
    error_type: str | None = None
    message: str | None = None
    job_id: str | None = None
    length_class: str | None = None
    execution_mode: str | None = None
    processing: dict | None = None
    target_processing: dict | None = None
    key: str = "body"
    name: str | None = None
    doc_name: str | None = None
    pages: int | None = None
    tokens: int | None = None
    characters: int | None = None
    block_count: int | None = None


@dataclass(frozen=True)
class IngestResult:
    source: str
    status: Literal["added", "skipped", "failed", "blocked", "partial", "stopped"]
    resources: tuple[str, ...]
    quality: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    input_version: str | None = None
    source_id: str | None = None
    source_revision_id: str | None = None
    units: tuple[ImportUnitOutcome, ...] = ()
    discovery_pending: int | None = None
    imports_pending: int | None = None
    message: str | None = None


def describe_ingest(result: IngestResult) -> tuple[str, ...]:
    """Plain-text projection shared by CLI add and watch."""
    if not result.source_id:
        return ()
    lines = [
        f"Source: {result.source_id}; revision: {result.source_revision_id}; {result.status}",
        f"Body units: {len(result.units)}; discovery pending: {result.discovery_pending}",
        f"File imports pending: {result.imports_pending}; continue with process-pending",
    ]
    if result.message:
        lines.append(result.message)
    if result.quality:
        lines.append("Quality: " + ", ".join(result.quality))
    if result.unfinished:
        lines.append("Unfinished: " + ", ".join(result.unfinished))
    return tuple(lines) + describe_units(result.units)


def describe_units(units: tuple[ImportUnitOutcome, ...]) -> tuple[str, ...]:
    lines = []
    for unit in units:
        lines.append(
            f"Unit: {unit.name or unit.doc_name or unit.key}; {unit.unit_id}; {unit.status}; "
            f"target: {unit.target_revision_id}; actual source: "
            f"{unit.successful_source_revision_id or 'none'}"
        )
        lines.append(
            f"Successful unit: {unit.successful_revision_id or 'none'}; "
            f"knowledge: {unit.knowledge_revision_id or 'none'}; view: {unit.view_id}"
        )
        if unit.message or unit.error_type:
            lines.append(f"Diagnostic: {unit.error_type or ''}; {unit.message or ''}")
        if unit.proposal_id:
            lines.append(f"Pending proposal: {unit.proposal_id}")
        if unit.block_count is not None:
            lines.append(f"Content blocks: {unit.block_count}")
        if unit.length_class:
            lines.append(f"Processing: {unit.length_class} / {unit.execution_mode}")
        if unit.processing:
            if unit.processing["measurement_unit"] == "token":
                lines.append(f"Text: {unit.processing['measurement_value']} tokens (cl100k_base)")
            elif unit.processing["measurement_unit"] == "physical_page":
                lines.append(f"Physical pages: {unit.processing['measurement_value']}")
            lines.append(
                f"Capacity: {unit.processing['capacity_status']}; "
                f"{unit.processing['capacity_reason'] or ''}"
            )
        if unit.target_processing and unit.target_processing != unit.processing:
            target = unit.target_processing
            lines.append(
                f"Target processing: {target['length_class']} / {target['execution_mode']}; "
                f"{target['measurement_value']} {target['measurement_unit']}; "
                f"capacity: {target['capacity_status']}; {target['capacity_reason']}"
            )
    return tuple(lines)
