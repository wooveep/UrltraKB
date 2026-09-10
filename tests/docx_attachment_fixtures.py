"""Small actual OPC/CFB containers for the embedded-document parsing seam."""

import struct
from zipfile import ZipFile

from tests.document_fixtures import write_docx


def compound_file(stream_name, payload):
    # One regular (>= 4096 byte) stream, one directory and one FAT sector.
    payload = payload.ljust(4096, b"\0")
    count = (len(payload) + 511) // 512
    assert count < 125
    header = bytearray(512)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 0, 0, 4096, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    struct.pack_into("<109I", header, 76, count + 1, *([0xFFFFFFFF] * 108))
    directory = bytearray(512)
    for index, (name, kind, first, size, child) in enumerate(
        [("Root Entry", 5, 0xFFFFFFFE, 0, 1), (stream_name, 2, 1, len(payload), 0xFFFFFFFF)]
    ):
        start = index * 128
        label = (name + "\0").encode("utf-16-le")
        directory[start : start + len(label)] = label
        struct.pack_into(
            "<HBBIII", directory, start + 64, len(label), kind, 1, 0xFFFFFFFF, 0xFFFFFFFF, child
        )
        struct.pack_into("<IQ", directory, start + 116, first, size)
    fat = [0xFFFFFFFE] + list(range(2, count + 1)) + [0xFFFFFFFE, 0xFFFFFFFD]
    fat += [0xFFFFFFFF] * (128 - len(fat))
    return (
        bytes(header + directory) + payload.ljust(count * 512, b"\0") + struct.pack("<128I", *fat)
    )


def native_package(name, payload):
    header = (
        struct.pack("<H", 2) + name.encode() + b"\0ignored-path\0" + b"\0" * 8 + b"ignored-temp\0"
    )
    body = header + struct.pack("<I", len(payload)) + payload
    return struct.pack("<I", len(body)) + body


def docx_with_parts(path, body, *, parts=None, relationships="", styles=""):
    write_docx(path, body)
    with ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries["word/_rels/document.xml.rels"] = entries["word/_rels/document.xml.rels"].replace(
        b"</Relationships>", relationships.encode() + b"</Relationships>"
    )
    entries["word/styles.xml"] = entries["word/styles.xml"].replace(
        b"</w:styles>", styles.encode() + b"</w:styles>"
    )
    entries.update(parts or {})
    with ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def attached_docx(path, payload):
    return docx_with_parts(
        path,
        "<w:p><w:r><w:t>Follow the attached instructions.</w:t><w:object>"
        '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'Type="Embed" ProgID="Word.Document.12" r:id="attachment1"/>'
        "</w:object></w:r></w:p>",
        parts={"word/embeddings/object.bin": compound_file("Package", payload)},
        relationships='<Relationship Id="attachment1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" '
        'Target="embeddings/object.bin"/>',
    )
