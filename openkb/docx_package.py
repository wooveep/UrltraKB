"""Expose embedded files at their original positions in Mammoth's document tree."""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
from dataclasses import dataclass
from xml.etree.ElementTree import Element, ParseError, SubElement, tostring
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from defusedxml import ElementTree as xml
from defusedxml.common import DefusedXmlException

from openkb.docx_containers import ExpansionBudget, package_path, read_member, unpack_ole
from openkb.sources import SourceStore, content_id

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
OFFICE = "{urn:schemas-microsoft-com:office:office}"
V = "{urn:schemas-microsoft-com:vml}"
_PARTS = {"word/document.xml", "word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}


@dataclass(frozen=True)
class Attachment:
    part: str
    name: str
    content: bytes
    container: str
    blob: str
    extraction_error: str | None = None


@dataclass
class PreparedDocx:
    stream: io.BytesIO
    attachments: dict[str, Attachment]
    icons: set[str]
    quality: list[dict[str, str]]


def prepare_docx(
    data: bytes, store: SourceStore, budget: ExpansionBudget, depth: int
) -> PreparedDocx:
    attachments: dict[str, Attachment] = {}
    icons: set[str] = set()
    quality: list[dict[str, str]] = []
    changed: dict[str, bytes] = {}
    stream = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        from openkb.parsing_failures import DocumentContentError

        if not {"[Content_Types].xml", "_rels/.rels"} <= set(names):
            raise DocumentContentError("docx_required_package_part_missing")
        package_relationships = xml.fromstring(
            read_member(archive, "_rels/.rels", budget, depth), forbid_dtd=True
        )
        try:
            main_parts = [
                package_path("", rel.get("Target", ""))
                for rel in package_relationships
                if rel.get("Type", "").endswith("/officeDocument")
                and rel.get("TargetMode", "Internal") == "Internal"
            ]
        except ValueError as exc:
            raise DocumentContentError("docx_main_document_target_invalid") from exc
        if len(main_parts) != 1 or main_parts[0] not in names:
            raise DocumentContentError("docx_main_document_part_missing")
        if len(names) != len(set(names)):
            raise ValueError("docx_duplicate_package_member")
        for part in sorted(_PARTS.intersection(names)):
            raw = read_member(archive, part, budget, depth)
            try:
                tree = xml.fromstring(raw, forbid_dtd=True)
            except (ParseError, DefusedXmlException):
                if part == "word/document.xml":
                    raise
                # Keep the body readable if an optional comment/footnote part is
                # malformed. The immutable original remains available for review.
                root = posixpath.basename(part).removesuffix(".xml")
                changed[part] = tostring(Element(W + root), encoding="utf-8", xml_declaration=True)
                quality.append({"status": "needs_review", "reason": "docx_part_unparsed:" + part})
                continue
            parents = {child: parent for parent in tree.iter() for child in parent}
            rel_path = posixpath.join(
                posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
            )
            relationships = {}
            if rel_path in names:
                rels = xml.fromstring(
                    read_member(archive, rel_path, budget, depth), forbid_dtd=True
                )
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
                    return store.put_bytes(
                        read_member(archive, internal(image.get(R + "id")), budget, depth)
                    )
                except (ValueError, KeyError):
                    return None

            def icon_label(node) -> str | None:
                from openkb.docx_labels import icon_filename

                labels = set()
                for shape in parents[node].iter(V + "shape"):
                    digest = image_digest(shape)
                    if digest:
                        icons.add(digest)
                        label = icon_filename(store.asset(digest).read_bytes())
                        if label:
                            labels.add(label)
                return labels.pop() if len(labels) == 1 else None

            def replace_object(node, text):
                parent = parents[node]
                label = Element(W + "t")
                label.text, label.tail = text, node.tail
                parent.insert(list(parent).index(node), label)
                parent.remove(node)
                # Remove this object's icon, not other occurrences of the same image.
                for shape in list(parent.iter(V + "shape")):
                    if shape.find(V + "imagedata") is not None:
                        parents[shape].remove(shape)

            changed_part = False
            for index, node in enumerate(list(tree.iter(OFFICE + "OLEObject")), 1):
                member = None
                display_name = None
                container = None
                try:
                    if node.get("Type") != "Embed":
                        raise ValueError("docx_linked_object_has_no_content")
                    member = internal(node.get(R + "id"))
                    container = read_member(archive, member, budget, depth + 1)
                    original = hashlib.sha256(container).hexdigest()
                    store.put_bytes(container)
                    name, content = unpack_ole(container)
                    display_name = name
                    budget.admit(len(content), depth + 1)
                    blob = hashlib.sha256(content).hexdigest()
                    from openkb.docx_attachments import attachment_name

                    stored_name = attachment_name(member, name, content)
                    if name is None:
                        label = icon_label(node)
                        if (
                            label
                            and posixpath.splitext(label)[1].lower()
                            == posixpath.splitext(stored_name)[1]
                        ):
                            stored_name = label
                    attachment = Attachment(member, stored_name, content, original, blob)
                    # Saving an embedded file never validates or parses its contents.
                    store.put_bytes(content)
                    marker = "[openkb-attachment-" + content_id([part, index, original]) + "]"
                    if marker.encode() in raw:
                        raise ValueError("docx_attachment_marker_collision")
                    attachments[marker] = attachment
                    icon_label(node)
                    replace_object(node, marker)
                    changed_part = True
                except (ValueError, KeyError, BadZipFile, ParseError, DefusedXmlException) as exc:
                    reason = str(exc) if str(exc).startswith("docx_") else "docx_attachment_missing"
                    if node.get("Type") == "Embed":
                        from openkb.attachments import filename_text

                        fallback_name = (
                            icon_label(node)
                            or display_name
                            or (posixpath.basename(member) if member else "未命名附件")
                        )
                        if container is not None and member is not None:
                            marker = (
                                "[openkb-attachment-" + content_id([part, index, original]) + "]"
                            )
                            if marker.encode() in raw:
                                raise ValueError("docx_attachment_marker_collision") from exc
                            attachments[marker] = Attachment(
                                member, fallback_name, container, original, original, reason
                            )
                            replace_object(node, marker)
                        else:
                            quality.append({"status": "needs_review", "reason": reason})
                            replace_object(node, filename_text(fallback_name))
                        changed_part = True
                    else:
                        quality.append({"status": "needs_review", "reason": reason})
            changed_part = _inline_vml_pictures(tree, relationships) or changed_part
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


def _inline_vml_pictures(tree, relationships):
    """Keep simple raster occurrences inside their owning OOXML paragraph.

    Mammoth emits w:pict as paragraph extras, losing its position even when the
    XML proves it is inline. Normalize only this unambiguous subset in the
    private input copy. Mixed textboxes, unknown transforms and floating shapes
    retain the existing unresolved-position path; no nearby paragraph is guessed.
    """
    wp = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    pic = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"
    parents = {child: parent for parent in tree.iter() for child in parent}
    declared_types = {"#" + node.get("id", "") for node in tree.iter(V + "shapetype")}
    changed = False
    for picture in list(tree.iter(W + "pict")):
        run = parents.get(picture)
        paragraph = parents.get(run)
        if run is None or paragraph is None or run.tag != W + "r" or paragraph.tag != W + "p":
            continue
        images = []
        for shape in picture:
            if shape.get("type") in declared_types:
                break  # A named picture frame can still inherit custom geometry.
            image = _simple_inline_vml(shape, relationships)
            if image is None:
                break
            images.append(image)
        if not images or len(images) != len(picture):
            continue
        position = list(run).index(picture)
        for offset, image in enumerate(images):
            drawing = Element(W + "drawing")
            inline = SubElement(drawing, wp + "inline")
            properties = {"id": str(offset + 1), "name": ""}
            if OFFICE + "title" in image.attrib:
                properties["title"] = image.get(OFFICE + "title")
            SubElement(inline, wp + "docPr", properties)
            graphic = SubElement(inline, a + "graphic")
            data = SubElement(graphic, a + "graphicData")
            raster = SubElement(data, pic + "pic")
            fill = SubElement(raster, pic + "blipFill")
            SubElement(fill, a + "blip", {R + "embed": image.get(R + "id")})
            if offset == len(images) - 1:
                drawing.tail = picture.tail
            run.insert(position + offset, drawing)
        run.remove(picture)
        changed = True
    return changed


def _simple_inline_vml(shape, relationships):
    word = "{urn:schemas-microsoft-com:office:word}"
    image = shape.find(V + "imagedata")
    relationship = relationships.get(image.get(R + "id")) if image is not None else None
    allowed = {
        "id",
        "type",
        "style",
        "filled",
        "stroked",
        "coordsize",
        OFFICE + "spt",
        OFFICE + "preferrelative",
    }
    if (
        shape.tag != V + "shape"
        or image is None
        or len(shape.findall(V + "imagedata")) != 1
        or relationship is None
        or relationship.get("TargetMode", "Internal") != "Internal"
        or not relationship.get("Type", "").endswith("/image")
        or set(image.attrib) - {R + "id", OFFICE + "title"}
        or set(shape.attrib) - allowed
        or shape.get("type", "#_x0000_t75") != "#_x0000_t75"
        or shape.get(OFFICE + "spt", "75") != "75"
        or shape.get("filled", "f") not in {"f", "false"}
        or shape.get("stroked", "f") not in {"f", "false"}
        or (shape.text or "").strip()
        or any(
            not re.fullmatch(
                r"(?:height|width)\s*:\s*[0-9]+(?:\.[0-9]+)?(?:pt|px|in|cm|mm)?",
                value.strip(),
                re.IGNORECASE,
            )
            for value in shape.get("style", "").split(";")
            if value.strip()
        )
    ):
        return None
    for node in shape:
        if (
            node.tag
            not in {
                V + "imagedata",
                V + "path",
                V + "fill",
                V + "stroke",
                OFFICE + "lock",
                word + "wrap",
                word + "anchorlock",
            }
            or len(node)
            or (node.tag == V + "path" and node.attrib)
            or (node.tag in {V + "fill", V + "stroke"} and node.get("on") != "f")
            or (node.tag == word + "wrap" and node.get("type") != "none")
            or (node.text or "").strip()
            or (node.tail or "").strip()
        ):
            return None
    return image
