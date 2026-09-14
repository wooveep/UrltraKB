"""Original evidence read from PageIndex, validated against native parse identities."""

import copy
import hashlib

from openkb.evidence import EvidenceSlice, complete_read_bound, evidence_bounds


class IndexedEvidence:
    def __init__(self, original, pages):
        self.sources, self.version, self.parsed = (
            original.sources,
            original.version,
            original.parsed,
        )
        self.blocks = copy.deepcopy(original.blocks)
        self.identity = (self.version.source_id, self.version.id, self.parsed.id)
        if not isinstance(pages, list) or len(pages) != len(self.blocks):
            raise ValueError("PageIndex original ranges are incomplete")
        self.texts = {}
        for page, block in zip(pages, self.parsed.blocks, strict=True):
            text = page.get("content")
            if (
                page.get("page") != block.order + 1
                or not isinstance(text, str)
                or len(text) != block.chars
                or hashlib.sha256(text.encode()).hexdigest() != block.blob
            ):
                raise ValueError("PageIndex original text differs from its native source")
            self.texts[block.id] = text

    def preceding(self, reference, *, max_blocks, max_chars):
        from openkb.evidence_context import preceding_slices

        return preceding_slices(
            self, reference, self.identity, self.blocks, max_blocks=max_blocks, max_chars=max_chars
        )

    def complete_bound(self, reference):
        return complete_read_bound(self.blocks[reference.block_id])

    def read(self, reference, *, max_chars):
        block, end = evidence_bounds(reference, self.identity, self.blocks, max_chars)
        text = self.texts[block.id][reference.start : min(end, reference.start + max_chars)]
        following = reference.start + len(text)
        return EvidenceSlice(
            reference,
            text,
            copy.deepcopy(block.location),
            block.kind,
            block.assets,
            block.context,
            following if following < end else None,
            copy.deepcopy(block.context_data),
        )
