"""DOC compound object locations; complete package streams and standard child storages."""

import olefile

from openkb.cfb_helper.runtime import rebuild_storage
from openkb.pending.budget import BudgetWait
from openkb.pending.compound import native_payload
from openkb.pending.ooxml import Candidate

STANDARD_STREAMS = {
    "WordDocument",
    "Workbook",
    "Book",
    "PowerPoint Document",
    "\x01Ole10Native",
    "Package",
}


class CompoundObjects:
    def __init__(self, original):
        self.original = original
        self.compound = olefile.OleFileIO(original, raise_defects=olefile.DEFECT_INCORRECT)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.compound.close()

    def scan(self, meter, *, legacy=False):
        streams = self.compound.listdir(streams=True, storages=False)
        document_roots = {
            tuple(path[:-1]) for path in streams if len(path) > 1 and path[-1] == "WordDocument"
        }
        objects = {
            tuple(path[:-1]) for path in streams if len(path) > 1 and path[-1] in STANDARD_STREAMS
        }
        objects.update(
            tuple(path[:2]) for path in streams if len(path) > 2 and path[0] == "ObjectPool"
        )
        candidates = []
        for storage in sorted(objects):
            meter.check()
            # A recovered DOC owns its own discovery. Descending here would import
            # the same actual inner object again when that document is admitted.
            if any(storage[: len(root)] == root and storage != root for root in document_roots):
                continue
            parts = [path for path in streams if path[: len(storage)] == list(storage)]
            direct = {path[-1] for path in parts if len(path) == len(storage) + 1}
            size = sum(self.compound.get_size(path) for path in parts)
            key = "/".join(storage)
            candidates.append(
                Candidate(
                    key,
                    "cfb:" + key,
                    size,
                    None if direct & STANDARD_STREAMS else "private_object",
                    None
                    if direct & STANDARD_STREAMS
                    else "Compound storage has no supported standard payload",
                )
            )
        return candidates

    def read(self, candidate, meter):
        storage = candidate.key.split("/")
        if self.compound.exists([*storage, "WordDocument"]):
            return rebuild_storage(self.original, storage, meter), candidate.key
        for name in ("\x01Ole10Native", "Package"):
            path = [*storage, name]
            if self.compound.exists(path):
                size = self.compound.get_size(path)
                if size > meter.remaining:
                    raise BudgetWait("Decompression budget exhausted")
                value = self.compound.openstream(path).read()
                meter.consume(len(value))
                if len(value) != size:
                    raise ValueError("Embedded package stream is truncated")
                return (
                    native_payload(value) if name == "\x01Ole10Native" else (value, "package.bin")
                )
        return rebuild_storage(self.original, storage, meter), candidate.key
