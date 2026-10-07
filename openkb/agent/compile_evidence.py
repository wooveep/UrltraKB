"""Bounded original-source reads for long-document page generation.

Read the staged source once: every concurrent concept sees the same frozen bytes
in the selected compile view, never the current source from a different version.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from openkb.agent.page_response import PageResponseError

MAX_EVIDENCE_UNITS = 6
MAX_EVIDENCE_CHARS = 24000
EVIDENCE_PLAN_INSTRUCTION = """
For each long-document create/update page, include evidence_units: an array of
1-6 original physical page ordinals (or block ordinals for block documents),
selected from the navigation ranges. These are read before generation. Select
the exact API, configuration or procedure pages, not just the overview.
Summaries and generated navigation titles are not original factual evidence.
"""


def version_context(kb_dir: Path, scope) -> str:
    from openkb.source_catalog import read_record
    from openkb.view_records import KnowledgeView

    view = (
        read_record(kb_dir, "views", scope.view_id, KnowledgeView)
        if scope.view_id != "legacy"
        else KnowledgeView(view_id="legacy")
    )
    return (
        "\nVerified applicability from the selected knowledge view: "
        + json.dumps(
            {"product": view.product, "verified_applicable_versions": view.applicable_versions},
            ensure_ascii=False,
        )
        + ". Empty versions mean unconfirmed; filenames cannot override this metadata.\n"
    )


@dataclass(frozen=True)
class CompileEvidence:
    locator: str
    text: str
    previous_content: str = ""

    @property
    def message(self) -> dict:
        return {
            "role": "user",
            "content": (
                "Original evidence for this page (immutable compile input):\n"
                + self.locator
                + "\n"
                + (self.text or "[No original evidence selected]")
                + "\nUse exact source field names, commands, values and conditions. "
                "Preserve subject/configuration/condition/locator separately; do not combine "
                "rules for different subjects. Cite the physical page or block. "
                "Omit executable examples and precise details unsupported by this evidence; "
                "state the evidence gap instead. A filename or generated title is only a hint, "
                "never proof of the applicable product version."
            ),
        }

    def validate(self, content: str) -> None:
        """Check new examples; unchanged examples may cite earlier source revisions.

        This does not certify old generated content or prose. Revalidating those
        facts requires their own original sources, not only this document's pages.
        """
        previous = set(_code_examples(self.previous_content))
        code = "\n".join(block for block in _code_examples(content) if block not in previous)
        keys = set(re.findall(r"""(?:^|[\n,{])\s*["']?([A-Za-z_][\w.-]*)["']?\s*:""", code))
        missing = sorted(
            key
            for key in keys
            if not re.search(r"(?<![\w])" + re.escape(key) + r"(?![\w])", self.text)
        )
        if missing:
            raise PageResponseError(
                "page_unsupported_fields",
                "Example keys absent from original evidence: " + ", ".join(missing),
            )
        if code.strip() and not self.text.strip():
            raise PageResponseError(
                "page_missing_evidence", "No original evidence for code example"
            )


def _code_examples(content: str) -> list[str]:
    fenced = re.findall(r"```[^\n]*\n(.*?)```", content, re.S)
    inline = re.findall(r"(?<!`)`([^`\n]+)`(?!`)", content)
    # Plain field references are not executable examples; only validate inline
    # structured values. This includes JSON request bodies inside curl commands.
    return fenced + [value for value in inline if ":" in value]


class LongDocumentEvidence:
    def __init__(self, wiki_dir: Path, doc_name: str):
        self.raw = None
        self.path = ""
        self.digest = ""
        for suffix in (".content.json", ".json"):
            path = wiki_dir / "sources" / f"{doc_name}{suffix}"
            if not path.resolve().is_relative_to(wiki_dir.resolve()):
                raise ValueError("Compile source escapes selected knowledge view")
            if path.is_file():
                data = path.read_bytes()
                self.raw = json.loads(data)
                self.path = path.relative_to(wiki_dir).as_posix()
                self.digest = hashlib.sha256(data).hexdigest()
                break

    def select(self, item: dict, *, previous_content: str = "") -> CompileEvidence:
        units = item.get("evidence_units", [])
        if not units or self.raw is None:
            return CompileEvidence(self.path or "No retained original source", "", previous_content)
        spec = ",".join(map(str, units))
        if isinstance(self.raw, list):
            from openkb.source_pages import read_page_selection

            selected = read_page_selection(self.raw, spec)
            if set(selected["page_range"]) != set(units):
                raise PageResponseError("page_invalid_evidence", "Requested page does not exist")
            kind = "physical pages"
        else:
            from openkb.block_package import read_block_selection

            selected = read_block_selection(self.raw, spec)
            kind = "blocks"
        body = selected["content"]
        # Never silently cut an API table or code block in half.
        if len(body) > MAX_EVIDENCE_CHARS:
            raise PageResponseError("page_evidence_budget", "Select fewer original evidence units")
        return CompileEvidence(
            f"{self.path}; sha256={self.digest}; {kind}={spec}", body, previous_content
        )
