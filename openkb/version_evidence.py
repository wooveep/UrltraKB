"""Read anchored document titles, never product mentions in ordinary prose."""

import re
from pathlib import Path

from openkb.view_records import VersionCandidate

_TITLE = re.compile(
    r"^(?P<product>[\w][\w .+\-]{0,60}?)\s+"
    r"(?:(?P<versions>(?:v|version\s*)\d+(?:\.\d+)*"
    r"(?:\s*[,/]\s*(?:v|version\s*)?\d+(?:\.\d+)*)*)\s+)?"
    r"(?P<family>Installation\s+(?:Manual|Guide)|Operations\s+(?:Manual|Guide)|"
    r"User(?:'s)?\s+(?:Manual|Guide)|安装(?:手册|指南)|运维手册|用户(?:手册|指南))"
    r"(?:\s*\(?\s*(?:Document\s+|Revision\s+|文档修订\s*)?(?P<revision>R\d[\w.\-]*)\s*\)?)?$",
    re.IGNORECASE,
)


def pdf_title_candidates(path: Path) -> tuple[VersionCandidate, ...]:
    import pymupdf

    with pymupdf.open(path) as pdf:
        locations = {"pdf.metadata.title": (pdf.metadata or {}).get("title", "")}
        if len(pdf):
            lines = pdf[0].get_text().strip().splitlines()
            if lines:
                locations["pdf.page[1].title"] = lines[0].strip()
    candidates = []
    for location, title in locations.items():
        match = _TITLE.fullmatch(title.strip())
        if not match:
            continue
        purpose = match["family"].lower()
        purpose = (
            "installation"
            if purpose.startswith(("installation", "安装"))
            else "operations"
            if purpose.startswith(("operations", "运维"))
            else "user guide"
        )
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
    return tuple(candidates)
