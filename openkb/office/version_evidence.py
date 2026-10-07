"""Bounded native OOXML cover reads, without conversion, formulas, macros or external links."""

import posixpath
import re
from pathlib import Path
from zipfile import ZipFile

from defusedxml.ElementTree import fromstring

_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_MAX_PART = 16 * 1024 * 1024


def _xml(package, name):
    info = package.getinfo(name)
    if info.file_size > _MAX_PART:
        raise ValueError("Version evidence XML part exceeds bounded read limit")
    return fromstring(package.read(name), forbid_dtd=True)


def _targets(package, part):
    directory, name = posixpath.split(part)
    relationships = _xml(package, f"{directory}/_rels/{name}.rels")
    result = {}
    for relationship in relationships:
        if relationship.get("TargetMode") == "External":
            continue
        target = relationship.get("Target", "")
        target = posixpath.normpath(
            target.lstrip("/") if target.startswith("/") else posixpath.join(directory, target)
        )
        if target.startswith("../") or target == "..":
            raise ValueError("Version evidence relationship escapes package")
        result[relationship.get("Id")] = target
    return result


def _paragraphs(root, namespace, prefix):
    result = []
    for index, paragraph in enumerate(root.iter(namespace + "p"), 1):
        if paragraph.find(".//" + namespace + "p") is not None:
            continue  # Textbox ancestors otherwise duplicate their nested paragraphs.
        text = "".join(t.text or "" for t in paragraph.iter(namespace + "t")).strip()
        if text and text not in {value for _, value in result}:
            result.append((f"{prefix}.paragraph[{index}]", text))
        if len(result) >= 12:
            break
    return result


def native_cover_locations(path: Path, source_format: str):
    """Return metadata titles and separate covers (one per sheet; first slide only)."""
    with ZipFile(path) as package:
        titles = []
        if "docProps/core.xml" in package.namelist():
            root = _xml(package, "docProps/core.xml")
            title = root.find("{http://purl.org/dc/elements/1.1/}title")
            if title is not None and title.text:
                titles.append((f"{source_format}.metadata.title", title.text))
        if source_format == "docx":
            return titles, [
                _paragraphs(_xml(package, "word/document.xml"), _WORD, "docx.word/document.xml")
            ]
        if source_format == "pptx":
            part = "ppt/presentation.xml"
            root = _xml(package, part)
            slides = root.find(
                "{http://schemas.openxmlformats.org/presentationml/2006/main}sldIdLst"
            )
            if slides is None or not len(slides):
                return titles, []
            target = _targets(package, part).get(slides[0].get(_REL))
            if target is None:
                raise ValueError("First slide has no internal relationship")
            return titles, [_paragraphs(_xml(package, target), _DRAWING, f"pptx.slide[1].{target}")]
        part = "xl/workbook.xml"
        sheets = _xml(package, part).find(_SHEET + "sheets")
        targets = _targets(package, part)
        strings = []
        if "xl/sharedStrings.xml" in package.namelist():
            strings = [
                "".join(t.text or "" for t in item.iter(_SHEET + "t"))
                for item in _xml(package, "xl/sharedStrings.xml")
            ]
        covers = []
        for sheet in list(sheets if sheets is not None else ())[:32]:
            target = targets.get(sheet.get(_REL))
            if target is None:
                continue
            cover = []
            for cell in _xml(package, target).iter(_SHEET + "c"):
                address = cell.get("r", "")
                if (
                    not re.fullmatch(r"[A-L](?:[1-9]|1[0-2])", address)
                    or cell.find(_SHEET + "f") is not None
                ):
                    continue
                value = cell.find(_SHEET + "v")
                text = value.text if value is not None else None
                if cell.get("t") == "s" and text is not None:
                    text = strings[int(text)]
                elif cell.get("t") == "inlineStr":
                    text = "".join(t.text or "" for t in cell.iter(_SHEET + "t"))
                if text and text.strip():
                    cover.append((f"xlsx.sheet[{sheet.get('name')}].cell[{address}]", text.strip()))
            covers.append(cover)
        return titles, covers
