"""Office compound object locations; complete packages and standard child storages."""

import re

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
DOCUMENT_STREAMS = {"WordDocument", "Workbook", "Book", "PowerPoint Document"}


class ContainerRebuildRequired(ValueError):
    pass


class CompoundObjects:
    def __init__(self, original):
        self.original = original
        self.compound = olefile.OleFileIO(original, raise_defects=olefile.DEFECT_INCORRECT)
        self.presentation = None
        self.document_streams = DOCUMENT_STREAMS

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.compound.close()

    def scan(self, meter, *, legacy=False, policy="office-embedded-files-v3"):
        streams = self.compound.listdir(streams=True, storages=False)
        self.document_streams = (
            {"WordDocument"} if policy == "office-embedded-files-v2" else DOCUMENT_STREAMS
        )
        document_roots = {
            tuple(path[:-1])
            for path in streams
            if len(path) > 1 and path[-1] in self.document_streams
        }
        objects = {
            tuple(path[:-1]) for path in streams if len(path) > 1 and path[-1] in STANDARD_STREAMS
        }
        objects.update(
            tuple(path[:2]) for path in streams if len(path) > 2 and path[0] == "ObjectPool"
        )
        if policy == "office-embedded-files-v3":
            objects.update(
                tuple(path)
                for path in self.compound.listdir(streams=False, storages=True)
                if (len(path) == 1 and re.fullmatch(r"(?:MBD|LNK)[0-9A-Fa-f]{8}", path[0]))
                or (len(path) == 2 and path[0] == "ObjectPool")
            )
        candidates = []
        for storage in sorted(objects):
            meter.check()
            # A recovered document owns its discovery. Descending here would import
            # the same actual inner object again when that document is admitted.
            if any(storage[: len(root)] == root and storage != root for root in document_roots):
                continue
            parts = [path for path in streams if path[: len(storage)] == list(storage)]
            direct = {path[-1] for path in parts if len(path) == len(storage) + 1}
            size = sum(self.compound.get_size(path) for path in parts)
            key = "/".join(storage)
            outcome, diagnostic = None, None
            if not direct & STANDARD_STREAMS:
                if (
                    policy == "office-embedded-files-v3"
                    and len(storage) == 1
                    and re.fullmatch(r"LNK[0-9A-Fa-f]{8}", storage[0])
                ):
                    outcome, diagnostic = (
                        "external_reference",
                        "Linked OLE object is not downloaded",
                    )
                elif policy == "office-embedded-files-v3" and any(
                    name.startswith("\x02OlePres") for name in direct
                ):
                    outcome, diagnostic = "preview", "Cached OLE presentation has no supported file"
                else:
                    outcome, diagnostic = (
                        "private_object",
                        "Compound storage has no supported standard payload",
                    )
            candidates.append(
                Candidate(
                    key,
                    "cfb:" + key,
                    size,
                    outcome,
                    diagnostic,
                )
            )
        if policy == "office-embedded-files-v3" and self.compound.exists("PowerPoint Document"):
            from openkb.pending.ppt_records import PresentationObjects

            self.presentation = PresentationObjects(self.compound, meter)
            candidates.extend(self.presentation.scan(meter))
        return sorted(candidates, key=lambda item: item.key)

    def read(self, candidate, meter):
        if candidate.part.startswith("ppt:"):
            return self.presentation.read(candidate, meter)
        storage = candidate.key.split("/")
        if any(self.compound.exists([*storage, name]) for name in self.document_streams):
            return self.rebuild(storage, meter), candidate.key
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
        return self.rebuild(storage, meter), candidate.key

    def rebuild(self, storage, meter):
        try:
            return rebuild_storage(self.original, storage, meter)
        except BudgetWait:
            raise
        except Exception as exc:
            raise ContainerRebuildRequired(str(exc)) from exc
