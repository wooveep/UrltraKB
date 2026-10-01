"""Read anchored document titles, never product mentions in ordinary prose."""

import re
from pathlib import Path

from openkb.view_records import VersionCandidate

_TITLE = re.compile(
    r"^(?P<product>[\w][\w .+\-]{0,60}?)[\s\-]+"
    r"(?:(?P<versions>(?:v|version\s*)\d+(?:\.\d+)*"
    r"(?:\s*[,/]\s*(?:v|version\s*)?\d+(?:\.\d+)*)*)[\s\-]+)?"
    r"(?P<family>Installation\s+(?:Manual|Guide)|Operations\s+(?:Manual|Guide)|"
    r"User(?:'s)?\s+(?:Manual|Guide)|安装(?:手册|指南)|运维手册|用户(?:手册|指南)|"
    r"(?:Product\s+)?White\s+Paper|Best\s+Practices|(?:产品)?白皮书|最佳实践)"
    r"(?:\s*\(?\s*(?:Document\s+|Revision\s+|文档修订\s*)?(?P<revision>R\d[\w.\-]*)\s*\)?)?$",
    re.IGNORECASE,
)

_VERSION_LINE = re.compile(r"^(?:版本|适用版本|Version)\s*[:：]\s*(?:v)?(\d+(?:\.\d+)+)$", re.I)
_REVISION_LINE = re.compile(
    r"^(?:资料修订|文档修订|Document Revision)\s*[:：]\s*(R\d[\w.\-]*)$", re.I
)
_FAMILY_LINE = re.compile(
    r"^(?:Installation\s+(?:Manual|Guide)|Operations\s+(?:Manual|Guide)|"
    r"User(?:'s)?\s+(?:Manual|Guide)|安装(?:手册|指南)|运维手册|用户(?:手册|指南)|"
    r"(?:Product\s+)?White\s+Paper|Best\s+Practices|(?:产品)?白皮书|最佳实践)$",
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
    ):
        if purpose.startswith(prefixes):
            return family
    return None


def pdf_title_candidates(path: Path) -> tuple[VersionCandidate, ...]:
    import pymupdf

    with pymupdf.open(path) as pdf:
        locations = {"pdf.metadata.title": (pdf.metadata or {}).get("title", "")}
        lines = []
        if len(pdf):
            page = pdf[0]
            # Some verified covers place their title at the bottom. Bound by the
            # first page and twelve nonempty lines, with exact title contracts.
            cover = page.get_text()
            lines = [line.strip() for line in cover.splitlines() if line.strip()][:12]
            for index, line in enumerate(lines[:6]):
                if len(line) <= 120:
                    locations[f"pdf.page[1].line[{index + 1}]"] = line
                if index + 1 < len(lines) and len(line) + len(lines[index + 1]) <= 120:
                    locations[f"pdf.page[1].lines[{index + 1}:{index + 2}]"] = (
                        line + "\n" + lines[index + 1]
                    )
    candidates = []
    for location, title in locations.items():
        match = _TITLE.fullmatch(title.strip())
        if not match:
            continue
        purpose = _purpose(match["family"])
        fields = {"product": (match["product"],), "family": (purpose,)}
        if match["versions"]:
            fields["applicable_versions"] = tuple(re.findall(r"\d+(?:\.\d+)*", match["versions"]))
        if match["revision"]:
            fields["document_revision"] = (match["revision"],)
        for field, values in fields.items():
            candidates.append(
                VersionCandidate.model_validate(
                    {"field": field, "values": values, "location": location, "excerpt": title}
                )
            )
    # Verified family-first covers put product immediately below the purpose.
    if (
        len(lines) >= 2
        and _FAMILY_LINE.fullmatch(lines[0])
        and re.fullmatch(r"[A-Za-z][\w .+\-]{0,60}", lines[1])
    ):
        excerpt = "\n".join(lines[:2])
        for field, value in (("product", lines[1]), ("family", _purpose(lines[0]))):
            candidates.append(
                VersionCandidate(
                    field=field, values=(value,), location="pdf.page[1].lines[1:2]", excerpt=excerpt
                )
            )
    if candidates:
        for index, line in enumerate(lines, 1):
            for pattern, field in (
                (_VERSION_LINE, "applicable_versions"),
                (_REVISION_LINE, "document_revision"),
            ):
                match = pattern.fullmatch(line)
                if match:
                    candidates.append(
                        VersionCandidate(
                            field=field,
                            values=(match[1],),
                            location=f"pdf.page[1].line[{index}]",
                            excerpt=line,
                        )
                    )
    return tuple(candidates)
