"""Recognize complete supported payloads from bytes before ordinary admission."""

import io
from pathlib import PurePosixPath
from zipfile import ZipFile

from defusedxml.ElementTree import fromstring

from openkb.pending.ooxml import read_part

MAIN_TYPES = {
    "wordprocessingml.document.main+xml": "docx",
    "presentationml.presentation.main+xml": "pptx",
    "spreadsheetml.sheet.main+xml": "xlsx",
}


def recognize(data, filename, meter, *, wrappers=0):
    meter.check()
    if wrappers > 8:
        raise ValueError("Excessive nested Package wrappers")
    if data.startswith(b"PK"):
        with ZipFile(io.BytesIO(data)) as package:
            names = package.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Ambiguous duplicate package parts")
            content_types = None
            for name in names:
                value = read_part(package, name, meter)
                if name == "[Content_Types].xml":
                    content_types = value
            if content_types:
                types = fromstring(content_types, forbid_dtd=True)
                matches = []
                for entry in types:
                    kind = (entry.get("ContentType") or "").removeprefix(
                        "application/vnd.openxmlformats-officedocument."
                    )
                    if kind in MAIN_TYPES and entry.get("PartName", "").lstrip("/") in names:
                        matches.append(MAIN_TYPES[kind])
                if len(matches) == 1:
                    return data, matches[0], "recovered", None
                if matches:
                    raise ValueError("Ambiguous Office package main part")
        return data, None, "private_object", "Package has no supported Office main part"
    if data.startswith(b"%PDF-"):
        import fitz

        with fitz.open(stream=data, filetype="pdf") as document:
            if document.is_repaired or document.page_count == 0:
                raise ValueError("Incomplete PDF payload")
        return data, "pdf", "recovered", None
    if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        return data, None, "preview", "Image representation is not a recoverable document"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        from openkb.pending.compound import inspect_compound

        payload, label, kind = inspect_compound(data, meter)
        if payload is not None and label is not None:
            return recognize(payload, label, meter, wrappers=wrappers + 1)
        if payload is not None:
            return payload, kind, "recovered", None
        reason = (
            "Standard document storage needs container reconstruction"
            if kind == "requires_container_rebuild"
            else "Compound object has no supported standard document"
        )
        return data, None, kind, reason
    extension = PurePosixPath(filename).suffix.lower().lstrip(".")
    if extension in {"md", "markdown", "txt", "csv", "xml", "html", "htm"}:
        from openkb.text_encoding import decode_text

        text, _, _ = decode_text(data)
        if any(ord(c) < 32 and c not in "\t\r\n\f" for c in text):
            raise ValueError("Text payload contains binary controls")
        return data, extension, "recovered", None
    return data, None, "private_object", "No supported complete document payload"
