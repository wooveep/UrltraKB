"""Expose embedded files at their original positions in Mammoth's document tree."""

from __future__ import annotations

import io
import posixpath
from dataclasses import dataclass
from zipfile import ZIP_DEFLATED, ZipFile

from defusedxml import ElementTree as xml
from xml.etree.ElementTree import Element, tostring

from openkb.docx_containers import ExpansionBudget, package_path, read_member, unpack_ole
from openkb.sources import SourceStore, content_id

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
O = "{urn:schemas-microsoft-com:office:office}"
V = "{urn:schemas-microsoft-com:vml}"
_PARTS = {"word/document.xml", "word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}


@dataclass(frozen=True)
class Attachment:
    part: str
    name: str
    content: bytes
    container: str
    blob: str


@dataclass
class PreparedDocx:
    stream: io.BytesIO
    attachments: dict[str, Attachment]
    icons: set[str]
    quality: list[dict[str, str]]


def prepare_docx(data: bytes, store: SourceStore, budget: ExpansionBudget, depth: int) -> PreparedDocx:
    attachments: dict[str, Attachment] = {}
    icons: set[str] = set()
    quality: list[dict[str, str]] = []
    changed: dict[str, bytes] = {}
    stream = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("docx_duplicate_package_member")
        for part in sorted(_PARTS.intersection(names)):
            raw = read_member(archive, part, budget, depth)
            tree = xml.fromstring(raw, forbid_dtd=True)
            parents = {child: parent for parent in tree.iter() for child in parent}
            rel_path = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
            relationships = {}
            if rel_path in names:
                rels = xml.fromstring(read_member(archive, rel_path, budget, depth), forbid_dtd=True)
                for rel in rels:
                    if rel.get("Id") in relationships:
                        raise ValueError("docx_duplicate_relationship")
                    relationships[rel.get("Id")] = rel

            def internal(reference: str | None) -> str:
                rel = relationships.get(reference)
                if rel is None or rel.get("TargetMode", "Internal") != "Internal":
                    raise ValueError("docx_attachment_relationship_unavailable")
                return package_path(part, rel.get("Target", ""))

            def image_digest(shape) -> str | None:
                image = shape.find(V + "imagedata")
                if image is None:
                    return None
                try:
                    return store.put_bytes(read_member(archive, internal(image.get(R + "id")), budget, depth))
                except (ValueError, KeyError):
                    return None

            changed_part = False
            for index, node in enumerate(list(tree.iter(O + "OLEObject")), 1):
                try:
                    if node.get("Type") != "Embed":
                        raise ValueError("docx_linked_object_has_no_content")
                    member = internal(node.get(R + "id"))
                    container = read_member(archive, member, budget, depth + 1)
                    name, content = unpack_ole(container)
                    budget.admit(len(content), depth + 1)
                    original, blob = store.put_bytes(container), store.put_bytes(content)
                    marker = "[openkb-attachment-" + content_id([part, index, original]) + "]"
                    if marker.encode() in raw:
                        raise ValueError("docx_attachment_marker_collision")
                    attachments[marker] = Attachment(member, name, content, original, blob)
                    parent = parents[node]
                    label = Element(W + "t")
                    label.text, label.tail = marker, node.tail
                    parent.insert(list(parent).index(node), label)
                    parent.remove(node)
                    for shape in parent.iter(V + "shape"):
                        digest = image_digest(shape)
                        if digest:
                            icons.add(digest)
                    changed_part = True
                except (ValueError, KeyError) as exc:
                    reason = str(exc) if str(exc).startswith("docx_") else "docx_attachment_missing"
                    quality.append({"status": "needs_review", "reason": reason})
            # VML style/path properties only become advisory when their complete
            # image representation is present. Uncovered vector shapes still block.
            for shape in tree.iter(V + "shape"):
                if not image_digest(shape):
                    continue
                for node in list(shape):
                    if node.tag in {V + "path", V + "fill", V + "stroke"}:
                        shape.remove(node)
                        changed_part = True
                        reason = "docx_conversion_warning:Preserved VML image; ignored " + node.tag
                        quality.append({"status": "verified", "reason": reason})
            if changed_part:
                changed[part] = tostring(tree, encoding="utf-8", xml_declaration=True)
        if not changed:
            return PreparedDocx(io.BytesIO(data), attachments, icons, quality)
        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as output:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                value = changed.get(entry.filename)
                if value is None:
                    value = read_member(archive, entry.filename, budget, depth)
                output.writestr(entry.filename, value)
    stream.seek(0)
    return PreparedDocx(stream, attachments, icons, quality)
