"""Read complete compound files and their standard Package payload without activation."""

import io
import struct

import olefile

from openkb.pending.budget import BudgetWait


def native_payload(data):
    """MS-OLEDS Ole10Native: bounded ANSI strings followed by a counted native file."""
    if len(data) < 8:
        raise ValueError("Truncated Ole10Native header")
    declared = struct.unpack_from("<I", data)[0]
    if declared > len(data) - 4:
        raise ValueError("Truncated Ole10Native record")
    end = 4 + declared
    offset = 6  # NativeDataSize and WORD flags.

    def string():
        nonlocal offset
        finish = data.find(b"\0", offset, end)
        if finish < 0:
            raise ValueError("Unterminated Ole10Native string")
        value = data[offset:finish].decode("cp1252", errors="replace")
        offset = finish + 1
        return value

    filename = string()
    string()  # Original path is only a label, never a filesystem target.
    offset += 8  # Reserved DWORDs.
    string()
    if offset + 4 > end:
        raise ValueError("Ole10Native has no native file size")
    size = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    if size > end - offset:
        raise ValueError("Ole10Native file is truncated")
    return data[offset : offset + size], filename


def inspect_compound(data, meter):
    with olefile.OleFileIO(io.BytesIO(data), raise_defects=olefile.DEFECT_INCORRECT) as compound:
        paths = compound.listdir(streams=True, storages=False)
        special = {}
        for path in paths:
            size = compound.get_size(path)
            if size > meter.remaining:
                raise BudgetWait("Compound stream exceeds decompression budget")
            value = compound.openstream(path).read()
            meter.consume(len(value))
            if len(value) != size:
                raise ValueError("Compound stream is truncated")
            if len(path) == 1:
                special[path[0]] = value
        if "\x01Ole10Native" in special:
            payload, filename = native_payload(special["\x01Ole10Native"])
            return payload, filename, None
        if "Package" in special:
            return special["Package"], "package.bin", None
        kinds = [
            extension
            for stream, extension in (
                ("WordDocument", "doc"),
                ("Workbook", "xls"),
                ("Book", "xls"),
                ("PowerPoint Document", "ppt"),
            )
            if stream in special
        ]
        if len(set(kinds)) == 1:
            return data, None, kinds[0]
        if kinds:
            raise ValueError("Ambiguous compound document type")
        if any(
            path[-1] in {"WordDocument", "Workbook", "Book", "PowerPoint Document"}
            for path in paths
        ):
            return None, None, "requires_container_rebuild"
        return None, None, "private_object"
