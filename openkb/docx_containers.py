"""Read embedded Office containers as data; never activate an OLE object."""

from __future__ import annotations

import io
import posixpath
import struct
from pathlib import PurePosixPath
from zipfile import ZipFile

from openkb.processing import processing_checkpoint

MAX_EMBEDDED_BYTES = 128_000_000
MAX_EXPANDED_BYTES = 512_000_000
MAX_EMBEDDED_FILES = 4096
MAX_EMBEDDED_DEPTH = 8


class ExpansionBudget:
    def __init__(self):
        self.bytes = self.files = 0

    def admit(self, size: int, depth: int) -> None:
        processing_checkpoint("parsing")
        if depth > MAX_EMBEDDED_DEPTH:
            raise ValueError("docx_attachment_depth_exceeded")
        if size < 0 or size > MAX_EMBEDDED_BYTES:
            raise ValueError("docx_attachment_size_exceeded")
        self.bytes += size
        self.files += 1
        if self.bytes > MAX_EXPANDED_BYTES or self.files > MAX_EMBEDDED_FILES:
            raise ValueError("docx_attachment_expansion_exceeded")


def package_path(part: str, target: str) -> str:
    """Resolve an internal OPC relationship without accessing the filesystem."""
    from urllib.parse import unquote, urlsplit

    url = urlsplit(target)
    if url.scheme or url.netloc or url.query or url.fragment:
        raise ValueError("docx_external_attachment_unsupported")
    target = unquote(url.path)
    if "\\" in target or "\x00" in target:
        raise ValueError("docx_attachment_path_invalid")
    name = posixpath.normpath(
        target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(part), target)
    )
    if name == ".." or name.startswith("../") or name in {"", "."}:
        raise ValueError("docx_attachment_path_invalid")
    return name


def read_member(archive: ZipFile, name: str, budget: ExpansionBudget, depth: int) -> bytes:
    entries = [entry for entry in archive.infolist() if entry.filename == name]
    if len(entries) != 1 or entries[0].is_dir() or entries[0].flag_bits & 1:
        raise ValueError("docx_attachment_member_invalid")
    budget.admit(entries[0].file_size, depth)
    return archive.read(entries[0])


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
            if "\x00" not in text:
                return text
        except UnicodeDecodeError:
            continue
    raise ValueError("docx_attachment_text_encoding_unsupported")


def _native_package(data: bytes) -> tuple[str, bytes]:
    # MS-OLEDS 2.3.6: the outer u32 is the native-data byte count.
    # The Package payload contains terminated label/path fields followed by
    # a counted file payload. Paths are metadata and are never opened.
    if len(data) < 6 or struct.unpack_from("<I", data)[0] != len(data) - 4:
        raise ValueError("docx_ole_native_size_invalid")
    if struct.unpack_from("<H", data, 4)[0] != 2:
        raise ValueError("docx_ole_native_format_unsupported")
    offset = 6

    def string() -> bytes:
        nonlocal offset
        end = data.find(b"\0", offset, min(len(data), offset + 4096))
        if end < 0:
            raise ValueError("docx_ole_native_string_invalid")
        value, offset = data[offset:end], end + 1
        return value

    label = decode_text(string())
    string()  # Original path.
    offset += 8
    string()  # Temporary path.
    if offset + 4 > len(data):
        raise ValueError("docx_ole_link_has_no_content")
    size = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    if not size or size > MAX_EMBEDDED_BYTES or offset + size > len(data):
        raise ValueError("docx_ole_native_payload_invalid")
    # Some writers append metadata after the counted payload; it is not file data.
    return PurePosixPath(label.replace("\\", "/")).name, data[offset : offset + size]


def unpack_ole(data: bytes) -> tuple[str, bytes]:
    """Return a Package file or embedded OOXML document from a CFB container."""
    if len(data) > MAX_EMBEDDED_BYTES:
        raise ValueError("docx_attachment_size_exceeded")
    if data.startswith(b"PK\x03\x04"):
        return "embedded.docx", data
    if not data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        raise ValueError("docx_ole_container_unsupported")
    from xlrd.compdoc import CompDoc, CompDocError

    try:
        container = CompDoc(data, logfile=io.StringIO())
        package = container.get_named_stream("Package")
        if package is not None:
            if not package.startswith(b"PK\x03\x04"):
                raise ValueError("docx_ole_package_unsupported")
            return "embedded.docx", package
        native = container.get_named_stream("\x01Ole10Native")
        if native is not None:
            return _native_package(native)
    except (CompDocError, struct.error, IndexError, UnicodeError) as exc:
        raise ValueError("docx_ole_container_invalid") from exc
    raise ValueError("docx_ole_content_unsupported")
