"""Detached, validated source text for concurrent readers under an ingestion lease."""

import copy
import weakref
from pathlib import Path
from tempfile import gettempdir

from openkb.evidence import Evidence, EvidenceSlice, complete_read_bound, evidence_bounds
from openkb.locks import atomic_write_text, kb_read_lock
from openkb.processing import processing_checkpoint
from openkb.resource_checks import check_disk, resource_operation
from openkb.runtime.input_store import InputStore


class EvidenceSnapshot:
    """Capture on the lease owner; worker reads never access live KB files.

    The private text and metadata belong only to this source version. Returned
    locations are copies, so one worker cannot alter another worker's evidence.
    Publication still validates the source and assets under its original lease.
    """

    def __init__(self, reader):
        self._identity = (reader.version.source_id, reader.version.id, reader.parsed.id)
        self._blocks = copy.deepcopy(reader.blocks)
        self._storage = InputStore(Path(gettempdir()).resolve() / "openkb-evidence-snapshots")
        self._cleanup = weakref.finalize(self, self._storage.cleanup)
        try:
            with (
                kb_read_lock(reader.sources.kb_dir / ".openkb"),
                resource_operation("generation", "snapshot_source"),
            ):
                for block in self._blocks.values():
                    processing_checkpoint("generation")
                    check_disk(
                        self._storage.name,
                        4 * block.chars,
                        stage="generation",
                        operation="snapshot_source",
                    )
                    reference = Evidence(*self._identity, block.id)
                    bound = complete_read_bound(block)
                    view = reader.read(reference, max_chars=bound)
                    if len(view.text) != block.chars:
                        raise ValueError("Evidence span is missing")
                    atomic_write_text(Path(self._storage.name) / block.id, view.text)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._cleanup()

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

    def complete_bound(self, reference):
        return complete_read_bound(self._blocks[reference.block_id])

    def read(self, reference, *, max_chars):
        block, end = evidence_bounds(reference, self._identity, self._blocks, max_chars)
        # Each concurrent reader owns a private handle. Only the requested range
        # enters memory; completed source bodies stay in the detached snapshot.
        from openkb.resource_budget import check_memory, current_resources

        check_memory((end - reference.start) * 8, stage="evidence_read")

        def load():
            with (Path(self._storage.name) / block.id).open(encoding="utf-8", newline="") as stream:
                remaining = reference.start
                while remaining:
                    skipped = stream.read(min(remaining, 8192))
                    if not skipped:
                        raise ValueError("Evidence span is missing")
                    remaining -= len(skipped)
                return stream.read(min(end - reference.start, max_chars))

        resources = current_resources()
        key = (self._storage.name, block.id, reference.start, end, max_chars)
        text = resources.read(key, load) if resources else load()
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
            block.chars,
        )
