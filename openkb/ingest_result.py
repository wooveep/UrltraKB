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
    message: str | None = None


def describe_ingest(result: IngestResult) -> tuple[str, ...]:
    """Plain-text projection shared by CLI add and watch."""
    if not result.source_id:
        return ()
    lines = [
        f"Source: {result.source_id}; revision: {result.source_revision_id}; {result.status}",
        f"Body units: {len(result.units)}; discovery pending: {result.discovery_pending}",
    ]
    if result.message:
        lines.append(result.message)
    for unit in result.units:
        if unit.length_class:
            lines.append(f"Processing: {unit.length_class} / {unit.execution_mode}")
        if unit.processing:
            lines.append(
                f"Capacity: {unit.processing['capacity_status']}; "
                f"{unit.processing['capacity_reason'] or ''}"
            )
        if unit.target_processing and unit.target_processing != unit.processing:
            target = unit.target_processing
            lines.append(
                f"Target processing: {target['length_class']} / {target['execution_mode']}; "
                f"capacity: {target['capacity_status']}; {target['capacity_reason']}"
            )
    return tuple(lines)
