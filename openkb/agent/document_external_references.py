"""Read source-bound external mentions from one validated private plan on demand."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from openkb.agent.document_plan import from_dict, range_intervals
from openkb.agent.document_plan_annotations import ExternalReference
from openkb.evidence import ParseStore
from openkb.sources import SourceStore, content_id, valid_id


@dataclass(frozen=True)
class ReferenceView:
    reference: ExternalReference
    plan_digest: str
    source_id: str
    version_id: str
    parse_id: str
    status: str = "unlinked"


def iter_external_references(kb_dir: Path, plan_ref: dict[str, str]) -> Iterator[ReferenceView]:
    """Yield one plan's mentions; an unread target never supplies facts or link state."""
    required = {"recovery_key", "plan_digest", "source_id", "version_id", "parse_id"}
    if not isinstance(plan_ref, dict) or set(plan_ref) != required:
        raise ValueError("An exact accepted plan reference is required")
    for field in required:
        valid_id(plan_ref[field], source=field == "source_id")
    store = SourceStore(kb_dir)
    key = plan_ref["recovery_key"]
    path = store.owned_path(store.root / "compilation" / "recovery" / f"{key}-plan.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("Invalid external reference plan record")
    value = record.get("value")
    if (
        not isinstance(value, dict)
        or record.get("key") != key
        or record.get("kind") != "plan"
        or record.get("digest") != content_id(value)
        or record.get("digest") != plan_ref["plan_digest"]
    ):
        raise ValueError("Invalid external reference plan record")
    plan = from_dict(value)
    metadata = plan.metadata
    identity = record.get("input")
    if (
        metadata.get("protocol") not in {"document-plan-v2", "document-plan-v3"}
        or metadata.get("recovery_key") != key
        or not isinstance(identity, dict)
        or (identity.get("source"), identity.get("version"), identity.get("parse"))
        != (metadata.get("source_id"), metadata.get("version_id"), metadata.get("parse_id"))
        or any(
            metadata.get(field) != plan_ref[field]
            for field in ("source_id", "version_id", "parse_id")
        )
    ):
        raise ValueError("External reference plan identity mismatch")
    if metadata["protocol"] == "document-plan-v2":
        from openkb.agent.document_plan_proof_reader import verify_accepted_plan

        verify_accepted_plan(kb_dir, plan_ref, record)
    else:
        from openkb.agent.document_plan_proof_reader import verify_markdown_reference_proof

        verify_markdown_reference_proof(kb_dir, plan_ref, record)
    parsed = ParseStore(kb_dir).load(metadata["parse_id"])
    for reference in plan.external_references:
        pieces = []
        for value in reference.location:
            for index, start, end in range_intervals(value, parsed, "external reference"):
                block = parsed.blocks[index]
                try:
                    body = store.asset(block.blob).read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise ValueError("External reference source text unavailable") from exc
                pieces.append(body[start:end])
        if "\n".join(pieces) != reference.raw_quote:
            raise ValueError("External reference quote differs from source")
        yield ReferenceView(
            reference,
            plan_ref["plan_digest"],
            metadata["source_id"],
            metadata["version_id"],
            metadata["parse_id"],
        )


def plan_reference(plan: Any) -> dict[str, str]:
    """Make the explicit, digest-bound handle required by the lazy reader."""
    from openkb.agent.document_plan import to_dict

    metadata = plan.metadata
    return {
        "recovery_key": metadata["recovery_key"],
        "plan_digest": content_id(to_dict(plan)),
        "source_id": metadata["source_id"],
        "version_id": metadata["version_id"],
        "parse_id": metadata["parse_id"],
    }
