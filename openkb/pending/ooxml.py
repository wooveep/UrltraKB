"""Actual OOXML parts are object identities; relationships are supplementary locators."""

import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote
from zipfile import ZipFile

from defusedxml.ElementTree import fromstring

from openkb.pending.budget import BudgetWait

OFFICE_PACKAGE_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.oleObject",
    "application/vnd.ms-office.oleObject",
}


@dataclass(frozen=True)
class Candidate:
    key: str
    part: str | None
    size: int = 0
    outcome: str | None = None
    diagnostic: str | None = None


def read_part(package, part, meter):
    if package.getinfo(part).file_size > meter.remaining:
        raise BudgetWait("Decompression budget exhausted")
    chunks = bytearray()
    with package.open(part) as stream:
        while chunk := stream.read(65536):
            meter.consume(len(chunk))
            chunks.extend(chunk)
    return bytes(chunks)


class OOXMLObjects:
    def __init__(self, original):
        self.package = ZipFile(original)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.package.close()

    def scan(self, meter, *, legacy=False):
        package = self.package
        names = package.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Ambiguous duplicate container parts")
        if legacy:
            return [
                Candidate(name, name, package.getinfo(name).file_size)
                for name in sorted(names)
                if name.startswith("word/embeddings/") and not name.endswith("/")
            ]
        actual = set(names)
        candidates = {}

        def add(part):
            if part in actual and not part.endswith("/"):
                candidates[part] = Candidate(part, part, package.getinfo(part).file_size)
            else:
                candidates[part] = Candidate(
                    part,
                    None,
                    outcome="corrupt_object",
                    diagnostic="Referenced package part is missing",
                )

        for name in names:
            meter.check()
            if name.startswith(
                ("word/embeddings/", "ppt/embeddings/", "xl/embeddings/")
            ) and not name.endswith("/"):
                add(name)
        if "[Content_Types].xml" in actual:
            try:
                types = fromstring(
                    read_part(package, "[Content_Types].xml", meter), forbid_dtd=True
                )
            except BudgetWait:
                raise
            except Exception as exc:
                types = []
                candidates["[Content_Types].xml"] = Candidate(
                    "[Content_Types].xml",
                    None,
                    outcome="corrupt_object",
                    diagnostic=f"Invalid content types: {exc}",
                )
            defaults = {
                entry.get("Extension"): entry.get("ContentType")
                for entry in types
                if entry.tag.endswith("}Default")
            }
            for name in names:
                if defaults.get(PurePosixPath(name).suffix.lstrip(".")) in OFFICE_PACKAGE_TYPES:
                    add(name)
            for entry in types:
                if entry.get("PartName") and entry.get("ContentType") in OFFICE_PACKAGE_TYPES:
                    add(unquote(entry.get("PartName", "")).lstrip("/"))
        for name in names:
            if not name.endswith(".rels"):
                continue
            try:
                relations = fromstring(read_part(package, name, meter), forbid_dtd=True)
            except BudgetWait:
                raise
            except Exception as exc:
                candidates[name] = Candidate(
                    name, None, outcome="corrupt_object", diagnostic=f"Invalid relationships: {exc}"
                )
                continue
            for relation in relations:
                kind = (relation.get("Type") or "").rsplit("/", 1)[-1]
                if kind not in {"oleObject", "package"}:
                    continue
                target = relation.get("Target", "")
                if relation.get("TargetMode") == "External":
                    key = name + "#" + relation.get("Id", "")
                    candidates[key] = Candidate(
                        key,
                        None,
                        outcome="external_reference",
                        diagnostic="External object reference was retained; no network fetch",
                    )
                    continue
                directory = posixpath.dirname(posixpath.dirname(name))
                part = posixpath.normpath(posixpath.join(directory, unquote(target)))
                part = part.lstrip("/")
                if part.startswith("../") or not target:
                    key = name + "#" + relation.get("Id", "")
                    candidates[key] = Candidate(
                        key,
                        None,
                        outcome="corrupt_object",
                        diagnostic="Package target escapes the container",
                    )
                else:
                    add(part)
        return [candidates[key] for key in sorted(candidates)]

    def read(self, candidate, meter):
        return read_part(self.package, candidate.part, meter), candidate.key
