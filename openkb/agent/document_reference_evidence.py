"""Read bounded, exact source excerpts for reference checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from openkb.agent.document_reference_check import ReferenceCandidate
from openkb.evidence import Evidence, ParseStore
from openkb.sources import content_id


@dataclass(frozen=True)
class ReferenceEvidence:
    blocks: tuple[dict[str, Any], ...]
    receipts: tuple[dict[str, Any], ...]


class ReferenceEvidenceError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def read_reference_evidence(
    kb_dir: Any,
    source: Any,
    parsed: Any,
    window_evidence: dict[str, Any],
    candidates: list[ReferenceCandidate],
    *,
    reader: Any | None = None,
) -> ReferenceEvidence:
    """Read only located nodes from the validated ParseVersion, once per block."""
    needed: dict[int, set[str]] = {}
    present = set()
    for block in window_evidence.get("blocks", []):
        if not isinstance(block, dict) or type(block.get("order")) is not int:
            continue
        index = block["order"]
        if not 0 <= index < len(parsed.blocks) or block.get("id") != parsed.blocks[index].id:
            continue
        reference = block.get("reference") or {}
        if not isinstance(reference, dict):
            continue
        if (
            reference.get("start", 0) == 0
            and reference.get("end", parsed.blocks[index].chars) == parsed.blocks[index].chars
            and isinstance(block.get("text"), str)
            and len(block["text"]) == parsed.blocks[index].chars
        ):
            present.add(index)
    for candidate in candidates:
        for node in candidate.target_options:
            for index in range(node["start"], node["end"]):
                if index not in present and "attachment" not in parsed.blocks[index].location:
                    needed.setdefault(index, set()).add(candidate.reference_key)
    if not needed:
        return ReferenceEvidence((), ())
    try:
        reader = reader or ParseStore(kb_dir).reader(source, parsed)
    except (OSError, ValueError, KeyError, FileNotFoundError) as exc:
        raise ReferenceEvidenceError("reference_evidence_read_failed", str(exc)) from exc
    blocks: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for index in sorted(needed):
        block = parsed.blocks[index]
        reference = Evidence(source.source_id, source.id, parsed.id, block.id, 0, block.chars)
        try:
            view = reader.read(reference, max_chars=reader.complete_bound(reference))
        except (OSError, ValueError, KeyError, FileNotFoundError) as exc:
            raise ReferenceEvidenceError("reference_evidence_read_failed", str(exc)) from exc
        if not isinstance(view.text, str) or len(view.text) != block.chars:
            raise ReferenceEvidenceError(
                "reference_evidence_read_failed", "Incomplete source excerpt"
            )
        blocks.append(
            {
                "id": block.id,
                "order": index,
                "kind": block.kind,
                "text": view.text,
                "location": view.location,
                "assets": list(block.assets),
            }
        )
        receipts.append(
            {
                "source_id": source.source_id,
                "version_id": source.id,
                "parse_id": parsed.id,
                "range": {"block_index": index, "start_char": 0, "end_char": block.chars},
                "text_hash": content_id(view.text),
                "reference_keys": sorted(needed[index]),
                "complete": True,
                "reference": asdict(reference),
            }
        )
    return ReferenceEvidence(tuple(blocks), tuple(receipts))
