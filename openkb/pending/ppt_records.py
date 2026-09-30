"""MS-PPT ExOleObjStg records, including unreferenced and historical records."""

import struct
import zlib

from openkb.pending.budget import BudgetWait
from openkb.pending.ooxml import Candidate


def diagnostic(offset, outcome, reason):
    return Candidate(f"PowerPoint Document@{offset}", None, outcome=outcome, diagnostic=reason)


def persist_offsets(data, start, end, meter):
    """Read directory evidence for diagnostics, never as a recovery allowlist."""
    result = {}
    while start < end:
        meter.check()
        if start + 4 > end:
            raise ValueError("Truncated persist directory entry")
        descriptor = struct.unpack_from("<I", data, start)[0]
        identity, count = descriptor & 0xFFFFF, descriptor >> 20
        start += 4
        if not count or start + 4 * count > end:
            raise ValueError("Invalid persist directory entry length")
        for index in range(count):
            result.setdefault(identity + index, set()).add(struct.unpack_from("<I", data, start)[0])
            start += 4
    return result


class PresentationObjects:
    def __init__(self, compound, meter):
        size = compound.get_size("PowerPoint Document")
        # olefile buffers a stream when opening it. Bound that allocation first.
        if size > meter.remaining:
            raise BudgetWait("Presentation record stream exceeds decompression budget")
        self.data = compound.openstream("PowerPoint Document").read()
        meter.consume(len(self.data))
        if len(self.data) != size:
            raise ValueError("Truncated PowerPoint Document stream")

    def scan(self, meter):
        candidates = []
        ranges = [(0, len(self.data), 0, None)]
        objects, references, stored = [], {}, set()
        previews, links = set(), set()
        while ranges:
            offset, end, depth, owner = ranges.pop()
            while offset < end:
                meter.check()
                key = f"PowerPoint Document@{offset}"
                error = None
                if offset + 8 > end:
                    error = "Truncated PowerPoint record header"
                else:
                    version_instance, kind, length = struct.unpack_from("<HHI", self.data, offset)
                    if offset + 8 + length > end:
                        error = "PowerPoint record extends beyond its container"
                if error:
                    candidates.append(
                        Candidate(key, None, outcome="corrupt_object", diagnostic=error)
                    )
                    break
                version, instance = version_instance & 15, version_instance >> 4
                if kind == 0x1011:
                    stored.add(offset)
                    if version != 0 or instance not in {0, 1} or (instance == 1 and length < 4):
                        candidates.append(
                            Candidate(
                                key,
                                None,
                                outcome="corrupt_object",
                                diagnostic="Invalid ExOleObjStg header",
                            )
                        )
                    else:
                        size = (
                            struct.unpack_from("<I", self.data, offset + 8)[0]
                            if instance
                            else length
                        )
                        candidates.append(Candidate(key, f"ppt:{offset}", size))
                elif kind == 0x0FC3:
                    if version != 1 or instance != 0 or length != 24:
                        candidates.append(
                            diagnostic(offset, "corrupt_object", "Invalid ExOleObjAtom")
                        )
                    else:
                        fields = struct.unpack_from("<IIIIII", self.data, offset + 8)
                        objects.append((offset, owner, fields[1], fields[4]))
                elif kind == 0x0FC1:
                    previews.add(owner)
                    candidates.append(
                        diagnostic(offset, "preview", "Metafile representation is not a document")
                    )
                elif kind == 0x1772:
                    try:
                        values = persist_offsets(self.data, offset + 8, offset + 8 + length, meter)
                        for identity, locations in values.items():
                            references.setdefault(identity, set()).update(locations)
                    except BudgetWait:
                        raise
                    except ValueError as exc:
                        candidates.append(diagnostic(offset, "corrupt_object", str(exc)))
                if kind == 0x0FCE:
                    links.add(offset)
                    candidates.append(
                        diagnostic(
                            offset, "external_reference", "Linked OLE object is not downloaded"
                        )
                    )
                if version == 15 and kind != 0x1011:
                    if depth >= 64:
                        candidates.append(
                            Candidate(
                                key,
                                None,
                                outcome="corrupt_object",
                                diagnostic="Excessive record nesting",
                            )
                        )
                    else:
                        ranges.append(
                            (
                                offset + 8,
                                offset + 8 + length,
                                depth + 1,
                                offset if kind in {0x0FCC, 0x0FCE} else owner,
                            )
                        )
                offset += 8 + length
        for offset, owner, object_type, reference in objects:
            if owner in links:
                continue
            if object_type == 1:
                candidates.append(
                    diagnostic(offset, "external_reference", "Linked OLE object is not downloaded")
                )
            elif references.get(reference, set()) & stored:
                continue  # Actual storage has its own recovery/diagnostic checkpoint.
            elif reference:
                candidates.append(
                    diagnostic(offset, "corrupt_object", "OLE object references missing storage")
                )
            elif owner not in previews:
                candidates.append(
                    diagnostic(
                        offset, "private_object", "OLE object metadata has no stored document"
                    )
                )
        return candidates

    def read(self, candidate, meter):
        offset = int(candidate.part.removeprefix("ppt:"))
        version_instance, _, length = struct.unpack_from("<HHI", self.data, offset)
        start, end = offset + 8, offset + 8 + length
        if version_instance >> 4 == 0:
            return self.data[start:end], candidate.key
        expected = struct.unpack_from("<I", self.data, start)[0]
        if expected > meter.remaining:
            raise BudgetWait("Presentation object exceeds decompression budget")
        decompressor = zlib.decompressobj()
        output = bytearray()
        cursor = start + 4
        while cursor < end:
            block = self.data[cursor : min(cursor + 65536, end)]
            cursor += len(block)
            while block:
                meter.check()
                decoded = decompressor.decompress(block, min(65536, expected - len(output) + 1))
                meter.consume(len(decoded))
                output.extend(decoded)
                if len(output) > expected:
                    raise ValueError("Presentation object exceeds its declared decompressed size")
                block = decompressor.unconsumed_tail
                if decompressor.unused_data:
                    raise ValueError("Trailing bytes after compressed presentation object")
        if not decompressor.eof or len(output) != expected:
            raise ValueError("Incomplete compressed presentation object")
        return bytes(output), candidate.key
