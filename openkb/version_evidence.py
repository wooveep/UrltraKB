"""Read anchored document titles, never product mentions in ordinary prose."""

import re
from pathlib import Path

from openkb.view_records import VersionCandidate

EVIDENCE_POLICY = "anchored-title-v2"

_TITLE = re.compile(
    r"^(?P<product>[\w][\w .+\-]{0,60}?)[\s\-]+"
    r"(?:(?P<versions>(?:v|version\s*)\d+(?:\.\d+)*"
    r"(?:\s*[,/]\s*(?:v|version\s*)?\d+(?:\.\d+)*)*)[\s\-]+)?"
    r"(?P<family>Installation\s+(?:Manual|Guide)|Operations\s+(?:Manual|Guide)|"
    r"User(?:'s)?\s+(?:Manual|Guide)|安装(?:手册|指南)|运维手册|用户(?:手册|指南)|"
    r"(?:Product\s+)?White\s+Paper|Best\s+Practices|(?:产品)?白皮书|最佳实践|"
    r"(?:接口)?开发指南|(?:产品)?功能列表|(?:产品)?技术交流材料)"
    r"(?:\s*\(?\s*(?:Document\s+|Revision\s+|文档修订\s*)?(?P<revision>R\d[\w.\-]*)\s*\)?)?$",
    re.IGNORECASE,
)

_VERSION_LINE = re.compile(
    r"^(?:日期\s*[:：]\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s+)?"
    r"(?:版本|适用版本|Version)\s*[:：]\s*(?:v)?(\d+(?:\.\d+)+)$",
    re.I,
)
_PRODUCT_LINE = re.compile(r"^(?:产品(?:名称)?|Product)\s*[:：]\s*(.{1,80})$", re.I)
_REVISION_LINE = re.compile(
    r"^(?:资料修订|文档修订|Document Revision)\s*[:：]\s*(R\d[\w.\-]*)$", re.I
)
_FAMILY_LINE = re.compile(
    r"^(?:Installation\s+(?:Manual|Guide)|Operations\s+(?:Manual|Guide)|"
    r"User(?:'s)?\s+(?:Manual|Guide)|安装(?:手册|指南)|运维手册|用户(?:手册|指南)|"
    r"(?:Product\s+)?White\s+Paper|Best\s+Practices|(?:产品)?白皮书|最佳实践|"
    r"(?:接口)?开发指南|(?:产品)?功能列表|(?:产品)?技术交流材料)$",
    re.I,
)


def _purpose(value):
    purpose = value.lower()
    for prefixes, family in (
        (("installation", "安装"), "installation"),
        (("operations", "运维"), "operations"),
        (("user", "用户"), "user guide"),
        (("white", "product white", "白皮书", "产品白皮书"), "white paper"),
        (("best", "最佳实践"), "best practices"),
        (("开发指南", "接口开发指南"), "api guide"),
        (("功能列表", "产品功能列表"), "feature list"),
        (("技术交流", "产品技术交流"), "technical presentation"),
    ):
        if purpose.startswith(prefixes):
            return family
    return None


def _candidate(field, values, location, excerpt, confidence="verified"):
    return VersionCandidate(
        field=field,
        values=values,
        location=location,
        excerpt=excerpt,
        confidence=confidence,
        policy=EVIDENCE_POLICY,
    )


def _title_candidates(location, title, confidence="verified"):
    title = title.strip()
    if Path(title).suffix.lower() in {".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"}:
        title = Path(title).stem
        confidence = "hint"
    match = _TITLE.fullmatch(title)
    if not match:
        return []
    fields = {"product": (match["product"],), "family": (_purpose(match["family"]),)}
    if match["versions"]:
        fields["applicable_versions"] = tuple(re.findall(r"\d+(?:\.\d+)*", match["versions"]))
    if match["revision"]:
        fields["document_revision"] = (match["revision"],)
    return [
        _candidate(field, values, location, title, confidence) for field, values in fields.items()
    ]


def cover_candidates(lines):
    lines = [(location, text.strip()) for location, text in lines if text.strip()][:12]
    candidates = []
    for index, (location, text) in enumerate(lines):
        if len(text) <= 120:
            candidates.extend(_title_candidates(location, text))
        if index + 1 < len(lines):
            next_location, next_text = lines[index + 1]
            excerpt = text + "\n" + next_text
            if len(excerpt) <= 160:
                candidates.extend(_title_candidates(location + "+" + next_location, excerpt))
            if (
                index == 0
                and _FAMILY_LINE.fullmatch(text)
                and re.fullmatch(r"[\w][\w .+\-/]{0,79}", next_text)
            ):
                for field, value in (("product", next_text), ("family", _purpose(text))):
                    candidates.append(
                        _candidate(field, (value,), location + "+" + next_location, excerpt)
                    )
        explicit = _PRODUCT_LINE.fullmatch(text)
        if explicit:
            candidates.append(_candidate("product", (explicit[1],), location, text))
        elif _FAMILY_LINE.fullmatch(text):
            candidates.append(_candidate("family", (_purpose(text),), location, text))
    # Require a cover product anchor, not an arbitrary product mention in body text.
    if any(c.field == "product" and c.confidence == "verified" for c in candidates):
        for location, text in lines:
            for pattern, field in (
                (_VERSION_LINE, "applicable_versions"),
                (_REVISION_LINE, "document_revision"),
            ):
                match = pattern.fullmatch(text)
                if match:
                    candidates.append(_candidate(field, (match[1],), location, text))
    return candidates


def pdf_title_candidates(path: Path) -> tuple[VersionCandidate, ...]:
    import pymupdf

    with pymupdf.open(path) as pdf:
        candidates = _title_candidates("pdf.metadata.title", (pdf.metadata or {}).get("title", ""))
        if len(pdf):
            lines = [line.strip() for line in pdf[0].get_text().splitlines() if line.strip()][:12]
            candidates.extend(
                cover_candidates(
                    [(f"pdf.page[1].line[{i}]", line) for i, line in enumerate(lines, 1)]
                )
            )
    return tuple(candidates)


def source_title_candidates(
    path: Path, source_format: str, name: str = ""
) -> tuple[VersionCandidate, ...]:
    from xml.etree.ElementTree import ParseError
    from zipfile import BadZipFile

    from defusedxml.common import DefusedXmlException

    candidates: list[VersionCandidate] = []
    if source_format == "pdf":
        candidates.extend(pdf_title_candidates(path))
    elif source_format in {"docx", "xlsx", "pptx"}:
        from openkb.office.version_evidence import native_cover_locations

        try:
            titles, covers = native_cover_locations(path, source_format)
        except (BadZipFile, ParseError, DefusedXmlException, KeyError, IndexError, ValueError):
            titles, covers = [], []  # Main conversion reports malformed/unsupported input.
        for location, title in titles:
            candidates.extend(_title_candidates(location, title))
        for cover in covers:
            candidates.extend(cover_candidates(cover))
    if name:
        candidates.extend(_title_candidates("source.filename", name, "hint"))
    return tuple(candidates)
