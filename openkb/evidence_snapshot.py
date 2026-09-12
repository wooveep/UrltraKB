"""Detached, validated source text for concurrent readers under an ingestion lease."""

import copy
import json

from openkb.evidence import Evidence, EvidenceSlice, evidence_bounds
from openkb.locks import kb_read_lock
from openkb.processing import processing_checkpoint


class EvidenceSnapshot:
    """Capture on the lease owner; worker reads never access live KB files.

    The private text and metadata belong only to this source version. Returned
    locations are copies, so one worker cannot alter another worker's evidence.
    Publication still validates the source and assets under its original lease.
    """

    def __init__(self, reader):
        self._identity = (reader.version.source_id, reader.version.id, reader.parsed.id)
        self._blocks = copy.deepcopy(reader.blocks)
        self._text = {}
        with kb_read_lock(reader.sources.kb_dir / ".openkb"):
            for block in self._blocks.values():
                processing_checkpoint("generation")
                reference = Evidence(*self._identity, block.id)
                bound = max(
                    4096,
                    block.chars,
                    len(block.context)
                    + len(json.dumps(block.location, ensure_ascii=False))
                    + 64 * len(block.assets),
                )
                view = reader.read(reference, max_chars=bound)
                if len(view.text) != block.chars:
                    raise ValueError("Evidence span is missing")
                self._text[block.id] = view.text

    def preceding(self, reference, *, max_blocks, max_chars):
        from openkb.evidence_context import preceding_slices

        return preceding_slices(
            self,
            reference,
            self._identity,
            self._blocks,
            max_blocks=max_blocks,
            max_chars=max_chars,
        )

    def read(self, reference, *, max_chars):
        block, end = evidence_bounds(reference, self._identity, self._blocks, max_chars)
        text = self._text[block.id][reference.start : min(end, reference.start + max_chars)]
        following = reference.start + len(text)
        return EvidenceSlice(
            reference,
            text,
            copy.deepcopy(block.location),
            block.kind,
            block.assets,
            block.context,
            following if following < end else None,
        )
